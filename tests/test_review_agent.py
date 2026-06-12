"""Tests for agents.review_agent. GitHub and LLM clients are fully mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents import review_agent
from llm_client import CodeIssue


def make_issue(
    severity: str = "medium",
    file: str = "app.py",
    line: int = 10,
    category: str = "bug",
) -> CodeIssue:
    """Build a CodeIssue with sensible defaults for tests."""
    return CodeIssue(
        file=file,
        line=line,
        severity=severity,
        category=category,
        title=f"{severity} {category} issue",
        explanation="Something is wrong here.",
        suggestion="Fix it like this.",
    )


def test_calculate_score_applies_severity_weights() -> None:
    """Score is 100 minus critical*25 + high*10 + medium*5 + low*1."""
    issues = [
        make_issue("critical", line=1),
        make_issue("high", line=2),
        make_issue("medium", line=3),
        make_issue("low", line=4),
        make_issue("low", line=5),
    ]
    assert review_agent.calculate_score(issues) == 100 - (25 + 10 + 5 + 1 + 1)


def test_calculate_score_floors_at_zero() -> None:
    """The score never goes below zero, however many issues exist."""
    issues = [make_issue("critical", line=n) for n in range(5)]
    assert review_agent.calculate_score(issues) == 0
    assert review_agent.calculate_score([]) == 100


def test_deduplicate_keeps_higher_severity() -> None:
    """Same file + line + category collapses to the higher-severity issue."""
    low = make_issue("low", line=10, category="bug")
    high = make_issue("high", line=10, category="bug")
    result = review_agent.deduplicate_issues([low, high])
    assert result == [high]


def test_deduplicate_keeps_distinct_issues() -> None:
    """Different category or line on the same file is not a duplicate."""
    issues = [
        make_issue("low", line=10, category="bug"),
        make_issue("low", line=10, category="security"),
        make_issue("low", line=11, category="bug"),
    ]
    assert review_agent.deduplicate_issues(issues) == issues


def _file(name: str, changes: int) -> dict[str, Any]:
    """Build a reviewable PR file entry with the given size."""
    return {"filename": name, "status": "modified", "patch": "+x", "changes": changes}


def test_files_beyond_max_files_are_skipped() -> None:
    """Only the largest max_files files survive the cap."""
    files = [_file(f"f{n}.py", changes=n) for n in range(30)]
    selected = review_agent.select_files(files, max_files=20)
    assert len(selected) == 20
    assert all(f["changes"] >= 10 for f in selected)


def test_select_files_filters_skippable_entries() -> None:
    """Deleted and binary files never reach the analyzer."""
    files = [
        _file("keep.py", changes=5),
        {"filename": "gone.py", "status": "removed", "patch": "+x", "changes": 5},
        {"filename": "logo.png", "status": "modified", "changes": 5},
    ]
    selected = review_agent.select_files(files, max_files=20)
    assert [f["filename"] for f in selected] == ["keep.py"]


def _mock_pipeline(mocker: Any, issues: list[CodeIssue]) -> dict[str, AsyncMock]:
    """Patch every external call run_review makes; return the mocks."""
    return {
        "get_pull_request": mocker.patch(
            "agents.review_agent.github_client.get_pull_request",
            AsyncMock(return_value={"title": "Add feature", "body": "Desc", "changed_files": 1}),
        ),
        "get_pr_files": mocker.patch(
            "agents.review_agent.github_client.get_pr_files",
            AsyncMock(return_value=[_file("app.py", changes=3)]),
        ),
        "analyze": mocker.patch(
            "agents.review_agent.file_analyzer.analyze",
            AsyncMock(return_value=issues),
        ),
        "generate_summary": mocker.patch(
            "agents.review_agent.llm_client.generate_summary",
            AsyncMock(return_value="This PR adds a feature."),
        ),
        "post_review": mocker.patch(
            "agents.review_agent.github_client.post_review",
            AsyncMock(return_value=True),
        ),
    }


@pytest.mark.asyncio
async def test_run_review_posts_scored_review(mocker: Any) -> None:
    """The full pipeline scores findings and posts the review once."""
    mocks = _mock_pipeline(mocker, [make_issue("medium")])
    result = await review_agent.run_review("octo", "demo", 7)
    assert result["score"] == 95
    assert len(result["issues"]) == 1
    assert "**Score: 95/100**" in result["summary"]
    assert result["comments"][0]["path"] == "app.py"
    mocks["post_review"].assert_awaited_once()


@pytest.mark.asyncio
async def test_run_review_dry_run_does_not_post(mocker: Any) -> None:
    """Dry-run analyses everything but never posts to GitHub."""
    mocks = _mock_pipeline(mocker, [make_issue("low")])
    result = await review_agent.run_review("octo", "demo", 7, dry_run=True)
    assert result["score"] == 99
    mocks["post_review"].assert_not_awaited()
