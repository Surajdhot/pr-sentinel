"""Tests for github_client. Every HTTP call is mocked — no real API calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import github_client

SAMPLE_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    "+x = 1\n"
)


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient returning canned responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        """Store the canned responses and an empty call log."""
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        """Exit the async context manager without suppressing errors."""
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Record the call and pop the next canned response."""
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)


def _install(mocker: Any, responses: list[httpx.Response]) -> _FakeAsyncClient:
    """Patch httpx.AsyncClient and the GitHub token; return the fake client."""
    fake = _FakeAsyncClient(responses)
    mocker.patch("github_client.httpx.AsyncClient", return_value=fake)
    mocker.patch.object(github_client.config, "GITHUB_TOKEN", "test-token")
    return fake


@pytest.mark.asyncio
async def test_get_pr_diff_returns_raw_diff(mocker: Any) -> None:
    """get_pr_diff returns the raw diff and requests the diff media type."""
    fake = _install(mocker, [httpx.Response(200, text=SAMPLE_DIFF)])
    diff = await github_client.get_pr_diff("octo", "demo", 7)
    assert diff == SAMPLE_DIFF
    method, url, kwargs = fake.calls[0]
    assert method == "GET"
    assert url.endswith("/repos/octo/demo/pulls/7")
    assert kwargs["headers"]["Accept"] == "application/vnd.github.v3.diff"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_post_review_builds_correct_payload(mocker: Any) -> None:
    """post_review sends the review payload GitHub's reviews API expects."""
    fake = _install(mocker, [httpx.Response(200, json={"id": 1})])
    comments = [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "fix this"}]
    ok = await github_client.post_review("octo", "demo", 7, comments, "## Summary", 87)
    assert ok is True
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url.endswith("/repos/octo/demo/pulls/7/reviews")
    payload = kwargs["json"]
    assert payload["event"] == "COMMENT"
    assert payload["body"] == "## Summary"
    assert payload["comments"] == comments


@pytest.mark.asyncio
async def test_post_review_handles_403_without_raising(mocker: Any) -> None:
    """A 403 from GitHub is logged and reported as failure, never raised."""
    _install(mocker, [httpx.Response(403, json={"message": "Forbidden"})])
    ok = await github_client.post_review("octo", "demo", 7, [], "## Summary", 100)
    assert ok is False


@pytest.mark.asyncio
async def test_post_review_falls_back_to_summary_on_422(mocker: Any) -> None:
    """A 422 on inline comments retries the review without the comments."""
    fake = _install(
        mocker,
        [
            httpx.Response(422, json={"message": "Unprocessable Entity"}),
            httpx.Response(200, json={"id": 2}),
        ],
    )
    comments = [{"path": "app.py", "line": 9999, "side": "RIGHT", "body": "x"}]
    ok = await github_client.post_review("octo", "demo", 7, comments, "## Summary", 90)
    assert ok is True
    assert len(fake.calls) == 2
    assert "comments" not in fake.calls[1][2]["json"]


@pytest.mark.asyncio
async def test_get_pull_request_raises_typed_not_found(mocker: Any) -> None:
    """A 404 raises GitHubNotFoundError rather than a generic error."""
    _install(mocker, [httpx.Response(404, json={"message": "Not Found"})])
    with pytest.raises(github_client.GitHubNotFoundError):
        await github_client.get_pull_request("octo", "gone", 1)


@pytest.mark.asyncio
async def test_rate_limit_429_is_retried(mocker: Any) -> None:
    """A 429 is retried with backoff and the retry's response is returned."""
    fake = _install(
        mocker,
        [
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json={"title": "Fix bug"}),
        ],
    )
    pr = await github_client.get_pull_request("octo", "demo", 7)
    assert pr["title"] == "Fix bug"
    assert len(fake.calls) == 2
