"""Top-level review orchestration for PR Sentinel.

Coordinates the multi-agent pipeline for one pull request: fetch metadata
and files, filter and cap the file list, run every enabled focused
reviewer independently, synthesize their findings deterministically,
score the merged result, then format and post the review.
"""

from __future__ import annotations

import logging
from typing import Any

import config
import github_client
import llm_client
from agents import comment_builder, file_analyzer, reviewers, synthesis
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


async def _safe_narrative(
    synth: synthesis.SynthesisResult, pr: dict[str, Any]
) -> str:
    """Generate the AI overview, falling back to a static line on failure."""
    try:
        return await llm_client.generate_synthesis_narrative(
            synth.issues, synth.disagreements, synth.reviewer_status, pr
        )
    except ReviewFailedError as exc:
        logger.error("Narrative generation failed, using fallback: %s", exc)
        return f"Automated review completed with {len(synth.issues)} issue(s) found."


async def _handle_total_failure(
    owner: str,
    repo: str,
    pr_number: int,
    synth: synthesis.SynthesisResult,
    dry_run: bool,
) -> dict[str, Any]:
    """Report a review in which every reviewer failed; post no review.

    Outside dry-run a failure notice is posted on the PR so the failure
    is never silent; in dry-run nothing is posted to GitHub.
    """
    logger.error(
        "All reviewers failed for %s/%s#%d — not posting a review",
        owner,
        repo,
        pr_number,
    )
    if dry_run:
        logger.info("Dry run — skipping failure notice")
    else:
        await github_client.post_failed_review(
            owner, repo, pr_number, "this pull request (all reviewers failed)"
        )
    return {
        "score": None,
        "issues": [],
        "summary": "",
        "comments": [],
        "failed_reviewers": synth.failed_reviewers,
    }


def _review_result(
    synth: synthesis.SynthesisResult,
    score: int,
    summary: str,
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble run_review's result dict from the synthesized pieces."""
    return {
        "score": score,
        "issues": synth.issues,
        "summary": summary,
        "comments": comments,
        "failed_reviewers": synth.failed_reviewers,
    }


async def run_review(
    owner: str, repo: str, pr_number: int, dry_run: bool = False
) -> dict[str, Any]:
    """Run the multi-agent review pipeline for one pull request.

    Every enabled reviewer analyses the same selected files independently;
    agents.synthesis merges their findings deterministically before
    scoring and posting. When only some reviewers fail, the review is
    still posted with the coverage gap disclosed; when all fail, a
    failure notice is posted instead (never in dry-run) and the returned
    score is None.
    """
    logger.info("Starting review of %s/%s#%d (dry_run=%s)", owner, repo, pr_number, dry_run)
    pr = await github_client.get_pull_request(owner, repo, pr_number)
    files = await github_client.get_pr_files(owner, repo, pr_number)
    selected = select_files(files)
    results = await reviewers.run_all_reviewers(selected)
    synth = synthesis.synthesize(results)
    if results and all(result.failed for result in results):
        return await _handle_total_failure(owner, repo, pr_number, synth, dry_run)
    score = calculate_score(synth.issues)
    overview = await _safe_narrative(synth, pr)
    summary = comment_builder.build_summary(
        synth.issues,
        score,
        overview,
        failed_reviewers=synth.failed_reviewers,
        disagreements=synth.disagreements,
    )
    comments = comment_builder.build_inline_comments(synth.issues)
    if dry_run:
        logger.info("Dry run — skipping GitHub post")
    else:
        await github_client.post_review(owner, repo, pr_number, comments, summary, score)
    logger.info(
        "Review of %s/%s#%d finished: score %d, %d issue(s), %d failed reviewer(s)",
        owner, repo, pr_number, score, len(synth.issues), len(synth.failed_reviewers),
    )
    return _review_result(synth, score, summary, comments)
