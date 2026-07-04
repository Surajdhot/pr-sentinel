"""Async Groq client for PR Sentinel.

Every LLM call in the project lives in this module, implemented with the
official groq SDK and Llama 3.3. The reviewer model returns its findings as
a JSON object, which this module parses into CodeIssue objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import groq

import config

logger = logging.getLogger(__name__)


class ReviewFailedError(Exception):
    """Raised when the model could not analyse a file after all retries."""

    def __init__(self, filename: str, reason: str = "") -> None:
        """Store the failed filename so callers can report it on the PR."""
        self.filename = filename
        message = f"Review failed for {filename}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


@dataclass
class CodeIssue:
    """A single problem reported by a reviewer model.

    ``reviewer`` names the focused reviewer(s) that raised the issue; after
    synthesis it may hold several comma-joined names when reviewers agree.
    """

    file: str
    line: int
    severity: str
    category: str
    title: str
    explanation: str
    suggestion: str
    reviewer: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the issue as a plain dict for JSON serialisation."""
        return asdict(self)


_client: groq.AsyncGroq | None = None


def _get_client() -> groq.AsyncGroq:
    """Return a shared AsyncGroq client, creating it on first use."""
    global _client
    if _client is None:
        _client = groq.AsyncGroq(api_key=config.GROQ_API_KEY)
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
    """Call the Groq chat completions API with exponential-backoff retries.

    Makes one initial attempt plus one retry per delay in
    config.LLM_RETRY_DELAYS (2s/4s/8s), covering rate limits and transient
    errors. Raises ReviewFailedError once all attempts are exhausted.
    """
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, *config.LLM_RETRY_DELAYS)):
        if delay:
            logger.warning(
                "Retrying Groq call for %s in %.0fs (retry %d/%d)",
                context,
                delay,
                attempt,
                len(config.LLM_RETRY_DELAYS),
            )
            await asyncio.sleep(delay)
        try:
            return await _get_client().chat.completions.create(**request_kwargs)
        except groq.APIError as exc:
            last_error = exc
            logger.error("Groq call failed for %s: %s", context, exc)
    raise ReviewFailedError(context, str(last_error))


def _normalise(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """Coerce a model-provided enum value into the allowed set."""
    cleaned = str(value).strip().lower()
    if cleaned in allowed:
        return cleaned
    logger.warning("Model sent unexpected value %r, using %r", value, default)
    return default


def _coerce_issue(
    data: dict[str, Any],
    file_path: str,
    reviewer: config.ReviewerSpec | None = None,
) -> CodeIssue | None:
    """Build a CodeIssue from one raw issue dict, or None if malformed.

    When a reviewer spec is given, the category is normalised against that
    reviewer's allowed categories (falling back to its default category)
    and the issue is tagged with the reviewer's name.
    """
    allowed = reviewer.categories if reviewer else config.CATEGORIES
    default = reviewer.default_category if reviewer else "bug"
    try:
        return CodeIssue(
            file=str(data.get("file") or file_path),
            line=int(data["line"]),
            severity=_normalise(data["severity"], config.SEVERITIES, "medium"),
            category=_normalise(data["category"], allowed, default),
            title=str(data["title"]),
            explanation=str(data["explanation"]),
            suggestion=str(data.get("suggestion", "")),
            reviewer=reviewer.name if reviewer else "",
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Discarding malformed issue for %s: %s", file_path, exc)
        return None


def _parse_issues(
    content: str,
    file_path: str,
    reviewer: config.ReviewerSpec | None = None,
) -> list[CodeIssue]:
    """Parse the model's JSON response into a list of CodeIssue objects."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Could not parse model JSON for %s: %s", file_path, exc)
        return []
    raw_issues = payload.get("issues", []) if isinstance(payload, dict) else []
    if not isinstance(raw_issues, list):
        logger.warning("Model 'issues' field was not a list for %s", file_path)
        return []
    issues: list[CodeIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        issue = _coerce_issue(raw, file_path, reviewer)
        if issue is not None:
            issues.append(issue)
    return issues


def _reviewer_messages(
    reviewer: config.ReviewerSpec,
    file_path: str,
    file_diff: str,
    chunk_number: int,
    total_chunks: int,
) -> list[dict[str, str]]:
    """Build the system and user messages for one reviewer analysis call."""
    prompt = _load_prompt("reviewer_analysis").format(
        file_path=file_path,
        language=detect_language(file_path),
        chunk_number=chunk_number,
        total_chunks=total_chunks,
        diff_content=file_diff,
        allowed_categories=", ".join(reviewer.categories),
    )
    return [
        {"role": "system", "content": _load_prompt(reviewer.system_prompt)},
        {"role": "user", "content": prompt},
    ]


async def run_reviewer_analysis(
    reviewer: config.ReviewerSpec,
    file_path: str,
    file_diff: str,
    chunk_number: int = 1,
    total_chunks: int = 1,
) -> list[CodeIssue]:
    """Run one focused reviewer over one chunk of a file's diff.

    Args:
        reviewer: Spec of the reviewer whose prompt and categories to use.
        file_path: Path of the file under review.
        file_diff: The diff chunk to analyse.
        chunk_number: 1-based index of this chunk within the file.
        total_chunks: Total number of chunks the file was split into.

    Raises:
        ReviewFailedError: If the API call fails after all retries.
    """
    response = await _create_with_retry(
        f"{reviewer.name}:{file_path}",
        model=config.GROQ_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=config.LLM_TEMPERATURE,
        response_format={"type": "json_object"},
        messages=_reviewer_messages(
            reviewer, file_path, file_diff, chunk_number, total_chunks
        ),
    )
    content = response.choices[0].message.content or "{}"
    issues = _parse_issues(content, file_path, reviewer)
    logger.info(
        "%s reviewer reported %d issue(s) in %s (chunk %d/%d)",
        reviewer.name,
        len(issues),
        file_path,
        chunk_number,
        total_chunks,
    )
    return issues


async def analyze_file(
    file_path: str,
    file_diff: str,
    chunk_number: int = 1,
    total_chunks: int = 1,
) -> list[CodeIssue]:
    """Analyse one chunk of a file's diff and return the issues found.

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
    response = await _create_with_retry(
        file_path,
        model=config.GROQ_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=config.LLM_TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _load_prompt("system_prompt")},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content or "{}"
    issues = _parse_issues(content, file_path)
    logger.info(
        "Model reported %d issue(s) in %s (chunk %d/%d)",
        len(issues),
        file_path,
        chunk_number,
        total_chunks,
    )
    return issues


async def generate_summary(
    all_issues: list[CodeIssue], pr_metadata: dict[str, Any]
) -> str:
    """Generate the plain-English PR summary via the model.

    Raises:
        ReviewFailedError: If the API call fails after all retries.
    """
    prompt = _load_prompt("summary").format(
        pr_title=pr_metadata.get("title", ""),
        pr_description=pr_metadata.get("body") or "(no description)",
        total_files=pr_metadata.get("changed_files", 0),
        issues_json=json.dumps([issue.to_dict() for issue in all_issues], indent=2),
    )
    response = await _create_with_retry(
        "PR summary",
        model=config.GROQ_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=config.LLM_TEMPERATURE,
        messages=[
            {"role": "system", "content": _load_prompt("system_prompt")},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


async def generate_synthesis_narrative(
    issues: list[CodeIssue],
    disagreements: list[dict[str, Any]],
    reviewer_status: dict[str, bool],
    pr_metadata: dict[str, Any],
) -> str:
    """Generate the plain-English overview of the synthesised review.

    The narrative is prose only — the final issue list is merged
    deterministically in agents/synthesis.py and is never edited by the
    model, so line numbers and severities stay exactly as reported.

    Raises:
        ReviewFailedError: If the API call fails after all retries.
    """
    status = ", ".join(
        f"{name}: {'completed' if ok else 'FAILED'}"
        for name, ok in reviewer_status.items()
    )
    prompt = _load_prompt("synthesis").format(
        pr_title=pr_metadata.get("title", ""),
        pr_description=pr_metadata.get("body") or "(no description)",
        total_files=pr_metadata.get("changed_files", 0),
        reviewer_status=status or "(no reviewers ran)",
        issues_json=json.dumps([issue.to_dict() for issue in issues], indent=2),
        disagreements_json=json.dumps(disagreements, indent=2),
    )
    response = await _create_with_retry(
        "synthesis narrative",
        model=config.GROQ_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        temperature=config.LLM_TEMPERATURE,
        messages=[
            {"role": "system", "content": _load_prompt("system_prompt")},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()
