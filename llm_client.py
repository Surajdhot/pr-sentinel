"""Async Anthropic Claude client for PR Sentinel.

Every Claude API call in the project lives in this module, implemented with
the official anthropic SDK and tool use. Claude reports findings by calling
the report_code_issue tool; this module converts those calls into CodeIssue
objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import anthropic

import config

logger = logging.getLogger(__name__)


class ReviewFailedError(Exception):
    """Raised when Claude could not analyse a file after all retries."""

    def __init__(self, filename: str, reason: str = "") -> None:
        """Store the failed filename so callers can report it on the PR."""
        self.filename = filename
        message = f"Review failed for {filename}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


@dataclass
class CodeIssue:
    """A single problem reported by the reviewer model."""

    file: str
    line: int
    severity: str
    category: str
    title: str
    explanation: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        """Return the issue as a plain dict for JSON serialisation."""
        return asdict(self)


CODE_ISSUE_TOOL: dict[str, Any] = {
    "name": "report_code_issue",
    "description": "Call this for every bug, security issue, or problem found",
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "file path"},
            "line": {
                "type": "integer",
                "description": "line number in the new version of the file",
            },
            "severity": {"type": "string", "enum": list(config.SEVERITIES)},
            "category": {"type": "string", "enum": list(config.CATEGORIES)},
            "title": {"type": "string", "description": "short, specific title"},
            "explanation": {
                "type": "string",
                "description": "what the issue is and why it matters",
            },
            "suggestion": {
                "type": "string",
                "description": "concrete code fix — actual code when possible",
            },
        },
        "required": [
            "file",
            "line",
            "severity",
            "category",
            "title",
            "explanation",
            "suggestion",
        ],
    },
}

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Return a shared AsyncAnthropic client, creating it on first use."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


@lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    return (config.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def detect_language(file_path: str) -> str:
    """Guess the language of a file from its extension."""
    suffix = Path(file_path).suffix.lower()
    return config.LANGUAGE_BY_EXTENSION.get(suffix, "text")


async def _create_with_retry(context: str, **request_kwargs: Any) -> Any:
    """Call the Claude Messages API with exponential-backoff retries.

    Makes one initial attempt plus one retry per delay in
    config.LLM_RETRY_DELAYS (2s/4s/8s). Raises ReviewFailedError once all
    attempts are exhausted, or immediately when the model returns a
    refusal stop reason — claude-fable-5 can decline a request with HTTP
    200 and empty content, and retrying the same request will not help.
    """
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, *config.LLM_RETRY_DELAYS)):
        if delay:
            logger.warning(
                "Retrying Claude call for %s in %.0fs (retry %d/%d)",
                context,
                delay,
                attempt,
                len(config.LLM_RETRY_DELAYS),
            )
            await asyncio.sleep(delay)
        try:
            message = await _get_client().messages.create(**request_kwargs)
        except anthropic.APIError as exc:
            last_error = exc
            logger.error("Claude call failed for %s: %s", context, exc)
            continue
        if getattr(message, "stop_reason", None) == "refusal":
            raise ReviewFailedError(context, "the model refused this request")
        return message
    raise ReviewFailedError(context, str(last_error))


def _normalise(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """Coerce a model-provided enum value into the allowed set."""
    cleaned = str(value).strip().lower()
    if cleaned in allowed:
        return cleaned
    logger.warning("Model sent unexpected value %r, using %r", value, default)
    return default


def _extract_issues(message: Any, file_path: str) -> list[CodeIssue]:
    """Convert report_code_issue tool calls in a response into CodeIssue objects."""
    issues: list[CodeIssue] = []
    for block in message.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name != "report_code_issue":
            continue
        data = dict(block.input)
        try:
            issues.append(
                CodeIssue(
                    file=str(data.get("file") or file_path),
                    line=int(data["line"]),
                    severity=_normalise(data["severity"], config.SEVERITIES, "medium"),
                    category=_normalise(data["category"], config.CATEGORIES, "bug"),
                    title=str(data["title"]),
                    explanation=str(data["explanation"]),
                    suggestion=str(data.get("suggestion", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Discarding malformed issue for %s: %s", file_path, exc)
    return issues


async def analyze_file(
    file_path: str,
    file_diff: str,
    chunk_number: int = 1,
    total_chunks: int = 1,
) -> list[CodeIssue]:
    """Analyse one chunk of a file's diff and return the issues Claude reports.

    Args:
        file_path: Path of the file under review.
        file_diff: The diff chunk to analyse.
        chunk_number: 1-based index of this chunk within the file.
        total_chunks: Total number of chunks the file was split into.

    Raises:
        ReviewFailedError: If the API call fails after all retries.
    """
    prompt = _load_prompt("file_analysis").format(
        file_path=file_path,
        language=detect_language(file_path),
        chunk_number=chunk_number,
        total_chunks=total_chunks,
        diff_content=file_diff,
    )
    message = await _create_with_retry(
        file_path,
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        system=_load_prompt("system_prompt"),
        tools=[CODE_ISSUE_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )
    issues = _extract_issues(message, file_path)
    logger.info(
        "Claude reported %d issue(s) in %s (chunk %d/%d)",
        len(issues),
        file_path,
        chunk_number,
        total_chunks,
    )
    return issues


async def generate_summary(
    all_issues: list[CodeIssue], pr_metadata: dict[str, Any]
) -> str:
    """Generate the plain-English PR summary via Claude.

    Raises:
        ReviewFailedError: If the API call fails after all retries.
    """
    prompt = _load_prompt("summary").format(
        pr_title=pr_metadata.get("title", ""),
        pr_description=pr_metadata.get("body") or "(no description)",
        total_files=pr_metadata.get("changed_files", 0),
        issues_json=json.dumps([issue.to_dict() for issue in all_issues], indent=2),
    )
    message = await _create_with_retry(
        "PR summary",
        model=config.ANTHROPIC_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        system=_load_prompt("system_prompt"),
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts).strip()
