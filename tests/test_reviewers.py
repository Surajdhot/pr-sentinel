"""Tests for agents.reviewers. All LLM calls are mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import config
from agents import reviewers
from llm_client import CodeIssue, ReviewFailedError
from tests.conftest import make_file, make_issue


def _spec(name: str = "security") -> config.ReviewerSpec:
    """Look up one reviewer spec from the registry by name."""
    return next(spec for spec in config.REVIEWERS if spec.name == name)


@pytest.mark.asyncio
async def test_run_reviewer_collects_issues_across_files(mocker: Any) -> None:
    """One reviewer accumulates findings from every file it is given."""
    issue = make_issue(reviewer="security")
    run_mock = mocker.patch(
        "agents.reviewers.llm_client.run_reviewer_analysis",
        AsyncMock(return_value=[issue]),
    )
    files = [make_file("a.py"), make_file("b.py")]
    result = await reviewers.run_reviewer(_spec(), files)
    assert result.failed is False
    assert result.error == ""
    assert result.issues == [issue, issue]
    assert run_mock.await_count == 2
    first_call = run_mock.await_args_list[0]
    assert first_call.args == (_spec(), "a.py", "+x = 1")
    assert first_call.kwargs == {"chunk_number": 1, "total_chunks": 1}


@pytest.mark.asyncio
async def test_run_reviewer_sends_every_chunk(mocker: Any) -> None:
    """A multi-chunk diff produces one LLM call per chunk, numbered."""
    mocker.patch(
        "agents.reviewers.file_analyzer.split_diff_into_chunks",
        return_value=["chunk-one", "chunk-two"],
    )
    run_mock = mocker.patch(
        "agents.reviewers.llm_client.run_reviewer_analysis",
        AsyncMock(return_value=[]),
    )
    await reviewers.run_reviewer(_spec(), [make_file("a.py")])
    assert run_mock.await_count == 2
    numbers = [call.kwargs["chunk_number"] for call in run_mock.await_args_list]
    totals = [call.kwargs["total_chunks"] for call in run_mock.await_args_list]
    assert numbers == [1, 2]
    assert totals == [2, 2]


@pytest.mark.asyncio
async def test_run_reviewer_failure_keeps_partial_findings(mocker: Any) -> None:
    """A mid-run failure marks the result failed but keeps earlier issues."""
    issue = make_issue(reviewer="security")
    run_mock = mocker.patch(
        "agents.reviewers.llm_client.run_reviewer_analysis",
        AsyncMock(side_effect=[[issue], ReviewFailedError("b.py", "boom")]),
    )
    files = [make_file("a.py"), make_file("b.py"), make_file("c.py")]
    result = await reviewers.run_reviewer(_spec(), files)
    assert result.failed is True
    assert result.issues == [issue]
    assert "b.py" in result.error
    assert run_mock.await_count == 2  # c.py is never attempted


@pytest.mark.asyncio
async def test_one_failing_reviewer_does_not_cancel_the_others(mocker: Any) -> None:
    """run_all_reviewers isolates a failure to the reviewer that hit it."""

    def fake_run(
        spec: config.ReviewerSpec,
        filename: str,
        chunk: str,
        chunk_number: int = 1,
        total_chunks: int = 1,
    ) -> list[CodeIssue]:
        if spec.name == "style":
            raise ReviewFailedError(filename, "rate limited")
        return [make_issue(reviewer=spec.name)]

    mocker.patch(
        "agents.reviewers.llm_client.run_reviewer_analysis",
        AsyncMock(side_effect=fake_run),
    )
    results = await reviewers.run_all_reviewers([make_file("app.py")])
    by_name = {result.name: result for result in results}
    assert sorted(by_name) == sorted(config.ENABLED_REVIEWERS)
    assert by_name["style"].failed is True
    assert by_name["style"].issues == []
    for name in ("security", "architecture"):
        assert by_name[name].failed is False
        assert len(by_name[name].issues) == 1
        assert by_name[name].issues[0].reviewer == name


@pytest.mark.asyncio
async def test_disabled_reviewers_do_not_run(
    mocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only reviewers named in ENABLED_REVIEWERS are executed."""
    monkeypatch.setattr(config, "ENABLED_REVIEWERS", ("architecture",))
    run_mock = mocker.patch(
        "agents.reviewers.llm_client.run_reviewer_analysis",
        AsyncMock(return_value=[]),
    )
    assert [spec.name for spec in reviewers.enabled_reviewers()] == ["architecture"]
    results = await reviewers.run_all_reviewers([make_file("app.py")])
    assert [result.name for result in results] == ["architecture"]
    assert run_mock.await_args.args[0].name == "architecture"
