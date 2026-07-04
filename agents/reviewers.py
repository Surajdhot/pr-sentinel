"""Focused reviewer execution for the multi-agent pipeline.

Runs each enabled reviewer independently over the same set of changed
files. Reviewers run concurrently with respect to each other, but each
reviewer works through its files sequentially, keeping at most one
in-flight LLM request per reviewer. Failures are isolated: one reviewer's
error never aborts the others, and partial findings are preserved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import config
import llm_client
from agents import file_analyzer
from llm_client import CodeIssue, ReviewFailedError

logger = logging.getLogger(__name__)


@dataclass
class ReviewerResult:
    """Outcome of one focused reviewer across the whole pull request."""

    name: str
    display_name: str
    issues: list[CodeIssue] = field(default_factory=list)
    failed: bool = False
    error: str = ""


def enabled_reviewers() -> tuple[config.ReviewerSpec, ...]:
    """Return the registry entries enabled via config.ENABLED_REVIEWERS."""
    return tuple(
        spec for spec in config.REVIEWERS if spec.name in config.ENABLED_REVIEWERS
    )


async def _review_file(
    spec: config.ReviewerSpec, file_info: dict[str, Any]
) -> list[CodeIssue]:
    """Run one reviewer over every chunk of one file's diff.

    Raises:
        ReviewFailedError: If any chunk's LLM call fails after all retries.
    """
    filename = file_info.get("filename", "")
    patch = file_info.get("patch") or ""
    chunks = file_analyzer.split_diff_into_chunks(patch)
    issues: list[CodeIssue] = []
    for number, chunk in enumerate(chunks, start=1):
        issues.extend(
            await llm_client.run_reviewer_analysis(
                spec, filename, chunk, chunk_number=number, total_chunks=len(chunks)
            )
        )
    return issues


async def run_reviewer(
    spec: config.ReviewerSpec, files: list[dict[str, Any]]
) -> ReviewerResult:
    """Run one reviewer over all files, capturing failure instead of raising.

    On ReviewFailedError the result is marked failed but keeps every issue
    collected before the error, so partial work is never thrown away.
    """
    result = ReviewerResult(name=spec.name, display_name=spec.display_name)
    logger.info("%s reviewer starting on %d file(s)", spec.name, len(files))
    for file_info in files:
        try:
            result.issues.extend(await _review_file(spec, file_info))
        except ReviewFailedError as exc:
            logger.error("%s reviewer failed, keeping partial findings: %s", spec.name, exc)
            result.failed = True
            result.error = str(exc)
            break
    logger.info(
        "%s reviewer finished: %d issue(s), failed=%s",
        spec.name,
        len(result.issues),
        result.failed,
    )
    return result


async def run_all_reviewers(files: list[dict[str, Any]]) -> list[ReviewerResult]:
    """Run every enabled reviewer concurrently over the same files."""
    specs = enabled_reviewers()
    logger.info("Running %d reviewer(s): %s", len(specs), ", ".join(s.name for s in specs))
    return list(await asyncio.gather(*(run_reviewer(spec, files) for spec in specs)))
