"""Async GitHub API client for PR Sentinel.

Every GitHub API call in the project lives in this module, implemented with
httpx. 403, 404 and 422 responses map to typed exceptions, and 429 rate
limiting is retried with exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

_PER_PAGE = 100


class GitHubClientError(Exception):
    """Base error for GitHub API failures."""


class GitHubForbiddenError(GitHubClientError):
    """HTTP 403 — the token lacks permission for this operation."""


class GitHubNotFoundError(GitHubClientError):
    """HTTP 404 — the repository, PR or endpoint does not exist."""


class GitHubValidationError(GitHubClientError):
    """HTTP 422 — GitHub rejected the request payload."""


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    """Build the standard GitHub API request headers."""
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": config.GITHUB_API_VERSION,
    }


def _raise_for_status(response: httpx.Response, context: str) -> None:
    """Translate error status codes into typed exceptions."""
    detail = f"{context}: {response.text[:300]}"
    if response.status_code == 403:
        raise GitHubForbiddenError(f"GitHub returned 403 Forbidden for {detail}")
    if response.status_code == 404:
        raise GitHubNotFoundError(f"GitHub returned 404 Not Found for {detail}")
    if response.status_code == 422:
        raise GitHubValidationError(f"GitHub returned 422 Unprocessable for {detail}")
    if response.status_code >= 400:
        raise GitHubClientError(
            f"GitHub returned {response.status_code} for {detail}"
        )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Compute the backoff delay for a 429, honouring Retry-After if present."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return float(retry_after)
        except ValueError:
            logger.debug("Ignoring non-numeric Retry-After header: %r", retry_after)
    return config.GITHUB_RATE_LIMIT_BASE_DELAY * (2**attempt)


async def _request(
    method: str,
    path: str,
    *,
    accept: str = "application/vnd.github+json",
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one GitHub API request, retrying on 429 with exponential backoff."""
    url = f"{config.GITHUB_API_BASE}{path}"
    context = f"{method} {path}"
    async with httpx.AsyncClient(timeout=config.GITHUB_TIMEOUT_SECONDS) as client:
        for attempt in range(config.GITHUB_RATE_LIMIT_RETRIES + 1):
            response = await client.request(
                method, url, headers=_headers(accept), json=json_body, params=params
            )
            if response.status_code != 429:
                _raise_for_status(response, context)
                return response
            if attempt == config.GITHUB_RATE_LIMIT_RETRIES:
                break
            delay = _retry_delay(response, attempt)
            logger.warning(
                "GitHub rate limited (429) on %s — retrying in %.1fs", context, delay
            )
            await asyncio.sleep(delay)
    raise GitHubClientError(f"GitHub rate limit retries exhausted for {context}")


async def get_pull_request(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """Fetch pull request metadata (title, body, changed_files, ...)."""
    response = await _request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
    return response.json()


async def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    """Fetch the raw unified diff for a pull request."""
    response = await _request(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    return response.text


async def get_pr_files(owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetch the full list of changed files in a pull request (paginated)."""
    files: list[dict[str, Any]] = []
    page = 1
    while True:
        response = await _request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            params={"per_page": _PER_PAGE, "page": page},
        )
        batch = response.json()
        files.extend(batch)
        if len(batch) < _PER_PAGE:
            return files
        page += 1


async def post_review(
    owner: str,
    repo: str,
    pr_number: int,
    comments: list[dict[str, Any]],
    summary: str,
    score: int,
) -> bool:
    """Post the full review with inline comments on a pull request.

    Returns True on success. A 403 is logged and reported as False rather
    than raised, so a token without write access never crashes the pipeline.
    A 422 (usually an inline comment on a line outside the diff) falls back
    to posting the summary without inline comments.
    """
    path = f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    payload = {"body": summary, "event": "COMMENT", "comments": comments}
    try:
        await _request("POST", path, json_body=payload)
    except GitHubForbiddenError as exc:
        logger.error("Cannot post review (403) — check token scopes: %s", exc)
        return False
    except GitHubValidationError as exc:
        logger.warning(
            "GitHub rejected inline comments (422), posting summary only: %s", exc
        )
        return await _post_summary_only_review(path, summary)
    logger.info(
        "Posted review with %d inline comment(s), score %d/100, to %s/%s#%d",
        len(comments),
        score,
        owner,
        repo,
        pr_number,
    )
    return True


async def _post_summary_only_review(path: str, summary: str) -> bool:
    """Post a review containing only the summary body, no inline comments."""
    try:
        await _request("POST", path, json_body={"body": summary, "event": "COMMENT"})
    except GitHubClientError as exc:
        logger.error("Summary-only review also failed: %s", exc)
        return False
    logger.info("Posted summary-only review after inline comment rejection")
    return True


async def post_failed_review(
    owner: str, repo: str, pr_number: int, filename: str
) -> None:
    """Post a PR comment noting that analysis failed for one file."""
    body = (
        f"⚠️ **PR Sentinel** could not review `{filename}` — analysis failed "
        "after multiple retries. Please review this file manually."
    )
    try:
        await _request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json_body={"body": body},
        )
    except GitHubClientError as exc:
        logger.error("Failed to post failure notice for %s: %s", filename, exc)
