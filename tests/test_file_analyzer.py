"""Tests for agents.file_analyzer. All LLM and GitHub calls are mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import config
from agents import file_analyzer
from llm_client import ReviewFailedError
from tests.conftest import make_issue


def test_file_over_10000_lines_is_skipped() -> None:
    """Files with more changed lines than MAX_FILE_LINES are skipped."""
    file_info = {
        "filename": "big.py",
        "status": "modified",
        "patch": "+x = 1",
        "changes": config.MAX_FILE_LINES + 1,
    }
    skip, reason = file_analyzer.should_skip_file(file_info)
    assert skip is True
    assert str(config.MAX_FILE_LINES) in reason


def test_deleted_and_binary_files_are_skipped() -> None:
    """Deleted files and files without a text patch are skipped."""
    deleted = {"filename": "old.py", "status": "removed", "patch": "+x", "changes": 1}
    binary = {"filename": "logo.png", "status": "modified", "changes": 1}
    assert file_analyzer.should_skip_file(deleted) == (True, "file was deleted")
    skip, reason = file_analyzer.should_skip_file(binary)
    assert skip is True
    assert "binary" in reason


def test_normal_file_is_not_skipped() -> None:
    """A regular modified file with a patch is reviewable."""
    file_info = {
        "filename": "app.py",
        "status": "modified",
        "patch": "+x = 1",
        "changes": 12,
    }
    assert file_analyzer.should_skip_file(file_info) == (False, "")


def test_small_diff_is_a_single_chunk() -> None:
    """A diff under the line limit is returned whole, and empty input is empty."""
    patch = "@@ -1,2 +1,3 @@\n+x = 1\n+y = 2"
    assert file_analyzer.split_diff_into_chunks(patch, max_lines=10) == [patch]
    assert file_analyzer.split_diff_into_chunks("", max_lines=10) == []


def test_chunks_split_at_function_boundaries() -> None:
    """When a diff exceeds the limit, the split lands on a def boundary."""
    lines = [
        "@@ -1,20 +1,20 @@",
        "+def first():",
        "+    a = 1",
        "+    b = 2",
        "+    return a + b",
        "+",
        "+def second():",
        "+    c = 3",
        "+    d = 4",
        "+    return c + d",
    ]
    chunks = file_analyzer.split_diff_into_chunks("\n".join(lines), max_lines=8)
    assert len(chunks) == 2
    assert chunks[0].splitlines()[-1] == "+"
    assert chunks[1].splitlines()[0] == "+def second():"


def test_chunks_fall_back_to_blank_lines() -> None:
    """Without def/class boundaries, the split falls back to a blank line."""
    lines = [
        "@@ -1,9 +1,9 @@",
        "+a = 1",
        "+b = 2",
        "+c = 3",
        "+",
        "+d = 4",
        "+e = 5",
        "+f = 6",
        "+g = 7",
    ]
    chunks = file_analyzer.split_diff_into_chunks("\n".join(lines), max_lines=6)
    assert chunks[0].splitlines()[-1] == "+c = 3"
    assert chunks[1].splitlines()[0] == "+"


@pytest.mark.asyncio
async def test_review_failed_error_posts_failure_notice(mocker: Any) -> None:
    """A ReviewFailedError posts a failure notice on the PR, never raises."""
    mocker.patch(
        "agents.file_analyzer.llm_client.analyze_file",
        AsyncMock(side_effect=ReviewFailedError("app.py")),
    )
    post_failed = mocker.patch(
        "agents.file_analyzer.github_client.post_failed_review", AsyncMock()
    )
    file_info = {
        "filename": "app.py",
        "status": "modified",
        "patch": "+x = 1",
        "changes": 1,
    }
    issues = await file_analyzer.analyze("octo", "demo", 7, file_info)
    assert issues == []
    post_failed.assert_awaited_once_with("octo", "demo", 7, "app.py")


@pytest.mark.asyncio
async def test_review_failed_error_dry_run_skips_failure_notice(mocker: Any) -> None:
    """In dry-run a ReviewFailedError must not post anything to GitHub."""
    mocker.patch(
        "agents.file_analyzer.llm_client.analyze_file",
        AsyncMock(side_effect=ReviewFailedError("app.py")),
    )
    post_failed = mocker.patch(
        "agents.file_analyzer.github_client.post_failed_review", AsyncMock()
    )
    file_info = {
        "filename": "app.py",
        "status": "modified",
        "patch": "+x = 1",
        "changes": 1,
    }
    issues = await file_analyzer.analyze("octo", "demo", 7, file_info, dry_run=True)
    assert issues == []
    post_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyze_collects_issues_from_chunks(mocker: Any) -> None:
    """analyze forwards each chunk to the LLM and gathers all findings."""
    issue = make_issue()
    analyze_mock = mocker.patch(
        "agents.file_analyzer.llm_client.analyze_file",
        AsyncMock(return_value=[issue]),
    )
    file_info = {
        "filename": "app.py",
        "status": "modified",
        "patch": "@@ -1,2 +1,3 @@\n+x = 1",
        "changes": 1,
    }
    issues = await file_analyzer.analyze("octo", "demo", 7, file_info)
    assert issues == [issue]
    analyze_mock.assert_awaited_once_with(
        "app.py", "@@ -1,2 +1,3 @@\n+x = 1", chunk_number=1, total_chunks=1
    )
