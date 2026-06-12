"""Top-level review orchestration for PR Sentinel.

Coordinates the full pipeline for one pull request: fetch metadata and
files, filter and cap the file list, analyse each file, deduplicate and
score the findings, then format and post the review.
"""

from __future__ import annotations

import logging
from typing import Any

import config
import github_client
import llm_client
from agents import comment_builder, file_analyzer
from llm_client import CodeIssue, ReviewFailedError

logger = logging.getLogger(__name__)


def calculate_score(issues: list[CodeIssue]) -> int:
    """Compute the 0-100 review score from issue severities.

    Score is 100 minus the summed severity weights (critical 25, high 10,
    medium 5, low 1), floored at 0.
    """
    penalty = sum(
        config.SEVERITY_WEIGHTS.get(issue.severity, 0) for issue in issues
    )
    return max(0, 100 - penalty)


def deduplicate_issues(issues: list[CodeIssue]) -> list[CodeIssue]:
    """Collapse duplicate findings on the same file, line and category.

    When duplicates exist, the issue with the higher severity weight wins.
    Input order is preserved for the surviving issues.
    """
    best: dict[tuple[str, int, str], CodeIssue] = {}
    for issue in issues:
        key = (issue.file, issue.line, issue.category)
        current = best.get(key)
        if current is None or (
            config.SEVERITY_WEIGHTS.get(issue.severity, 0)
            > config.SEVERITY_WEIGHTS.get(current.severity, 0)
        ):
            best[key] = issue
    return list(best.values())


def select_files(
    files: list[dict[str, Any]], max_files: int | None = None
) -> list[dict[str, Any]]:
    """Filter out skippable files and cap the list at max_files.

    When the cap applies, the largest files by changed lines are kept,
    since they carry the most review surface.
    """
    limit = max_files or config.MAX_FILES_PER_PR
    reviewable: list[dict[str, Any]] = []
    for file_info in files:
        skip, reason = file_analyzer.should_skip_file(file_info)
        if skip:
            logger.info("Skipping %s: %s", file_info.get("filename"), reason)
        else:
            reviewable.append(file_info)
    if len(reviewable) <= limit:
        return reviewable
    logger.info(
        "PR has %d reviewable files — keeping the %d largest", len(reviewable), limit
    )
    return sorted(reviewable, key=lambda f: f.get("changes", 0), reverse=True)[:limit]


async def _safe_summary(issues: list[CodeIssue], pr: dict[str, Any]) -> str:
    """Generate the AI overview, falling back to a static line on failure."""
    try:
        return await llm_client.generate_summary(issues, pr)
    except ReviewFailedError as exc:
        logger.error("Summary generation failed, using fallback: %s", exc)
        return f"Automated review completed with {len(issues)} issue(s) found."


async def run_review(
    owner: str, repo: str, pr_number: int, dry_run: bool = False
) -> dict[str, Any]:
    """Run the full review pipeline for one pull request.

    Args:
        owner: Repository owner login.
        repo: Repository name.
        pr_number: Pull request number.
        dry_run: When True, analyse but do not post anything to GitHub.

    Returns:
        A dict with the score, deduplicated issues, summary markdown and
        the inline comment payloads.
    """
    logger.info("Starting review of %s/%s#%d (dry_run=%s)", owner, repo, pr_number, dry_run)
    pr = await github_client.get_pull_request(owner, repo, pr_number)
    files = await github_client.get_pr_files(owner, repo, pr_number)
    selected = select_files(files)
    issues: list[CodeIssue] = []
    for file_info in selected:
        issues.extend(
            await file_analyzer.analyze(
                owner, repo, pr_number, file_info, dry_run=dry_run
            )
        )
    issues = deduplicate_issues(issues)
    score = calculate_score(issues)
    overview = await _safe_summary(issues, pr)
    summary = comment_builder.build_summary(issues, score, overview)
    comments = comment_builder.build_inline_comments(issues)
    if dry_run:
        logger.info("Dry run — skipping GitHub post")
    else:
        await github_client.post_review(owner, repo, pr_number, comments, summary, score)
    logger.info(
        "Review of %s/%s#%d finished: score %d, %d issue(s)",
        owner,
        repo,
        pr_number,
        score,
        len(issues),
    )
    return {"score": score, "issues": issues, "summary": summary, "comments": comments}
