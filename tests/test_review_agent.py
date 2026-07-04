"""Tests for agents.review_agent. GitHub and LLM clients are fully mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents import review_agent
from agents.reviewers import ReviewerResult
from llm_client import CodeIssue
from tests.conftest import disagreement_results, make_file, make_issue


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


def test_files_beyond_max_files_are_skipped() -> None:
    """Only the largest max_files files survive the cap."""
    files = [make_file(f"f{n}.py", changes=n) for n in range(30)]
    selected = review_agent.select_files(files, max_files=20)
    assert len(selected) == 20
    assert all(f["changes"] >= 10 for f in selected)


def test_select_files_filters_skippable_entries() -> None:
    """Deleted and binary files never reach the reviewers."""
    files = [
        make_file("keep.py", changes=5),
        {"filename": "gone.py", "status": "removed", "patch": "+x", "changes": 5},
        {"filename": "logo.png", "status": "modified", "changes": 5},
    ]
    selected = review_agent.select_files(files, max_files=20)
    assert [f["filename"] for f in selected] == ["keep.py"]


def _results(issues: list[CodeIssue]) -> list[ReviewerResult]:
    """Wrap issues in a successful security result plus two empty ones."""
    return [
        ReviewerResult(name="security", display_name="Security", issues=issues),
        ReviewerResult(name="style", display_name="Style & Quality", issues=[]),
        ReviewerResult(name="architecture", display_name="Architecture", issues=[]),
    ]


def _mock_pipeline(
    mocker: Any, results: list[ReviewerResult]
) -> dict[str, AsyncMock]:
    """Patch every external call run_review makes; return the mocks."""
    return {
        "get_pull_request": mocker.patch(
            "agents.review_agent.github_client.get_pull_request",
            AsyncMock(return_value={"title": "Add feature", "body": "Desc", "changed_files": 1}),
        ),
        "get_pr_files": mocker.patch(
            "agents.review_agent.github_client.get_pr_files",
            AsyncMock(return_value=[make_file("app.py", changes=3)]),
        ),
        "run_all_reviewers": mocker.patch(
            "agents.review_agent.reviewers.run_all_reviewers",
            AsyncMock(return_value=results),
        ),
        "narrative": mocker.patch(
            "agents.review_agent.llm_client.generate_synthesis_narrative",
            AsyncMock(return_value="This PR adds a feature."),
        ),
        "post_review": mocker.patch(
            "agents.review_agent.github_client.post_review",
            AsyncMock(return_value=True),
        ),
        "post_failed_review": mocker.patch(
            "agents.review_agent.github_client.post_failed_review",
            AsyncMock(),
        ),
    }


@pytest.mark.asyncio
async def test_run_review_posts_scored_review(mocker: Any) -> None:
    """The full pipeline scores merged findings and posts the review once."""
    issues = [make_issue("medium", reviewer="security")]
    mocks = _mock_pipeline(mocker, _results(issues))
    result = await review_agent.run_review("octo", "demo", 7)
    assert result["score"] == 95
    assert len(result["issues"]) == 1
    assert result["failed_reviewers"] == []
    assert "**Score: 95/100**" in result["summary"]
    assert result["comments"][0]["path"] == "app.py"
    assert "Flagged by: security" in result["comments"][0]["body"]
    mocks["post_review"].assert_awaited_once()
    mocks["post_failed_review"].assert_not_awaited()


@pytest.mark.asyncio
async def test_run_review_dry_run_does_not_post(mocker: Any) -> None:
    """Dry-run analyses everything but never posts to GitHub."""
    mocks = _mock_pipeline(mocker, _results([make_issue("low", reviewer="style")]))
    result = await review_agent.run_review("octo", "demo", 7, dry_run=True)
    assert result["score"] == 99
    mocks["post_review"].assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_failure_still_posts_with_warning(mocker: Any) -> None:
    """One failed reviewer degrades the review, it does not abort it."""
    results = _results([make_issue("high", reviewer="security")])
    results[1].failed = True
    results[1].error = "rate limited"
    mocks = _mock_pipeline(mocker, results)
    result = await review_agent.run_review("octo", "demo", 7)
    assert result["failed_reviewers"] == ["Style & Quality"]
    assert "Partial review" in result["summary"]
    assert "(partial)" in result["summary"]
    mocks["post_review"].assert_awaited_once()
    mocks["post_failed_review"].assert_not_awaited()


@pytest.mark.asyncio
async def test_total_failure_posts_notice_not_review(mocker: Any) -> None:
    """When every reviewer fails, a failure notice replaces the review."""
    results = _results([])
    for result in results:
        result.failed = True
    mocks = _mock_pipeline(mocker, results)
    outcome = await review_agent.run_review("octo", "demo", 7)
    assert outcome["score"] is None
    assert outcome["comments"] == []
    assert len(outcome["failed_reviewers"]) == 3
    mocks["post_review"].assert_not_awaited()
    mocks["post_failed_review"].assert_awaited_once_with(
        "octo", "demo", 7, "this pull request (all reviewers failed)"
    )


@pytest.mark.asyncio
async def test_total_failure_dry_run_posts_nothing(mocker: Any) -> None:
    """Dry-run stays read-only even when every reviewer fails."""
    results = _results([])
    for result in results:
        result.failed = True
    mocks = _mock_pipeline(mocker, results)
    outcome = await review_agent.run_review("octo", "demo", 7, dry_run=True)
    assert outcome["score"] is None
    mocks["post_review"].assert_not_awaited()
    mocks["post_failed_review"].assert_not_awaited()


@pytest.mark.asyncio
async def test_disagreement_flows_through_to_the_posted_review(mocker: Any) -> None:
    """Both conflicting claims reach the comments, summary and narrative."""
    mocks = _mock_pipeline(mocker, disagreement_results())
    result = await review_agent.run_review("octo", "demo", 7)
    assert result["score"] == 74  # critical (25) + low (1), each counted once
    anchors = [(c["path"], c["line"]) for c in result["comments"]]
    assert anchors.count(("config_loader.py", 12)) == 2
    assert "Reviewers disagreed" in result["summary"]
    assert "config_loader.py:12" in result["summary"]
    disagreements_sent = mocks["narrative"].await_args.args[1]
    assert disagreements_sent[0]["file"] == "config_loader.py"
    mocks["post_review"].assert_awaited_once()
