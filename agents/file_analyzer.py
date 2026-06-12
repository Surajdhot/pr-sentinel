"""Per-file analysis for PR Sentinel: skip rules, smart chunking, LLM dispatch.

Splits a file's diff into chunks at natural boundaries (function/class
definitions, then blank lines), sends each chunk to the LLM, and posts a
failure notice on the PR if analysis fails after retries.
"""

from __future__ import annotations

import logging
from typing import Any

import config
import github_client
import llm_client
from llm_client import CodeIssue, ReviewFailedError

logger = logging.getLogger(__name__)

_DIFF_MARKERS = ("+", "-", " ")


def should_skip_file(file_info: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a changed file should be skipped entirely.

    Args:
        file_info: One entry from the GitHub PR files API.

    Returns:
        A (skip, reason) tuple; reason is empty when the file is reviewable.
    """
    if file_info.get("status") == "removed":
        return True, "file was deleted"
    if not file_info.get("patch"):
        return True, "binary file or no text diff available"
    if file_info.get("changes", 0) > config.MAX_FILE_LINES:
        return True, f"more than {config.MAX_FILE_LINES} changed lines"
    return False, ""


def _line_content(line: str) -> str:
    """Strip the leading diff marker (+/-/space) from a diff line."""
    if line[:1] in _DIFF_MARKERS:
        return line[1:]
    return line


def _is_boundary_line(line: str) -> bool:
    """Return True if a diff line starts a function/class or a new hunk."""
    if line.startswith("@@"):
        return True
    stripped = _line_content(line).lstrip()
    return stripped.startswith(("def ", "async def ", "class "))


def _best_split_index(lines: list[str], start: int, end: int) -> int:
    """Pick the best index in (start, end] at which the next chunk begins.

    Prefers the last function/class/hunk boundary inside the window so a
    function is never split when avoidable; falls back to the last blank
    line, then to a hard split at the window edge.
    """
    fallback_blank = -1
    for index in range(end, start, -1):
        line = lines[index]
        if _is_boundary_line(line):
            return index
        if fallback_blank == -1 and _line_content(line).strip() == "":
            fallback_blank = index
    if fallback_blank > start:
        return fallback_blank
    return end


def split_diff_into_chunks(patch: str, max_lines: int | None = None) -> list[str]:
    """Split a unified diff into chunks of at most max_lines lines.

    Args:
        patch: The file's unified diff text.
        max_lines: Chunk size limit; defaults to config.MAX_LINES_PER_CHUNK.

    Returns:
        A list of diff chunks, in order. Empty input yields an empty list.
    """
    limit = max_lines or config.MAX_LINES_PER_CHUNK
    lines = patch.splitlines()
    if not lines:
        return []
    if len(lines) <= limit:
        return [patch]
    chunks: list[str] = []
    start = 0
    while start < len(lines):
        end = min(start + limit, len(lines))
        if end < len(lines):
            end = _best_split_index(lines, start, end)
        chunks.append("\n".join(lines[start:end]))
        start = end
    return chunks


async def analyze(
    owner: str, repo: str, pr_number: int, file_info: dict[str, Any]
) -> list[CodeIssue]:
    """Analyse one changed file and return all issues found in it.

    Splits the diff into chunks, sends each chunk to the LLM, and on a
    ReviewFailedError posts a failure notice on the PR and returns whatever
    was collected from earlier chunks.
    """
    filename = file_info.get("filename", "")
    patch = file_info.get("patch") or ""
    chunks = split_diff_into_chunks(patch)
    total = len(chunks)
    issues: list[CodeIssue] = []
    logger.info("Analyzing %s in %d chunk(s)", filename, total)
    for number, chunk in enumerate(chunks, start=1):
        try:
            issues.extend(
                await llm_client.analyze_file(
                    filename, chunk, chunk_number=number, total_chunks=total
                )
            )
        except ReviewFailedError as exc:
            logger.error("Analysis failed for %s: %s", filename, exc)
            await github_client.post_failed_review(owner, repo, pr_number, filename)
            break
    return issues
