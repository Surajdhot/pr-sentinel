"""Formats review findings into GitHub inline comments and the summary body."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import config
from llm_client import CodeIssue, detect_language

logger = logging.getLogger(__name__)

CATEGORY_LABELS: dict[str, str] = {
    "bug": "Bug",
    "security": "Security",
    "performance": "Performance",
    "style": "Style",
    "error_handling": "Error handling",
}

_FOOTER = "*Reviewed by PR Sentinel — powered by Claude*"


def _issue_body(issue: CodeIssue) -> str:
    """Render the markdown body for one inline comment."""
    category = CATEGORY_LABELS.get(issue.category, issue.category.title())
    body = (
        f"**[{issue.severity.upper()}] {category}: {issue.title}**\n\n"
        f"{issue.explanation}\n"
    )
    if issue.suggestion:
        language = detect_language(issue.file)
        body += f"\n**Suggested fix:**\n```{language}\n{issue.suggestion}\n```\n"
    return body


def build_inline_comments(issues: list[CodeIssue]) -> list[dict[str, Any]]:
    """Format all issues as GitHub review comment payloads.

    Each issue becomes a comment anchored to its line on the new (RIGHT)
    side of the diff, matching GitHub's POST /pulls/{n}/reviews schema.
    """
    return [
        {
            "path": issue.file,
            "line": issue.line,
            "side": "RIGHT",
            "body": _issue_body(issue),
        }
        for issue in issues
    ]


def _severity_table(issues: list[CodeIssue]) -> list[str]:
    """Render the severity count table rows."""
    counts = Counter(issue.severity for issue in issues)
    rows = ["| Severity | Count |", "|----------|-------|"]
    for severity in config.SEVERITIES:
        rows.append(f"| {severity.title()} | {counts.get(severity, 0)} |")
    return rows


def _grouped_issue_lines(issues: list[CodeIssue]) -> list[str]:
    """List issues grouped by file, ordered by severity then line number."""
    if not issues:
        return ["No issues found — nice work!"]
    by_file: dict[str, list[CodeIssue]] = {}
    for issue in issues:
        by_file.setdefault(issue.file, []).append(issue)
    lines: list[str] = []
    for filename in sorted(by_file):
        lines.append(f"#### `{filename}`")
        ordered = sorted(
            by_file[filename],
            key=lambda i: (-config.SEVERITY_WEIGHTS.get(i.severity, 0), i.line),
        )
        for issue in ordered:
            lines.append(
                f"- **[{issue.severity.upper()}]** line {issue.line} — {issue.title}"
            )
        lines.append("")
    return lines


def build_summary(issues: list[CodeIssue], score: int, overview: str) -> str:
    """Render the top-level review summary comment.

    Args:
        issues: All deduplicated issues found in the PR.
        score: The 0-100 review score.
        overview: The AI-generated plain-English summary of the PR.
    """
    parts: list[str] = ["## PR Sentinel Review", ""]
    if overview:
        parts.extend([overview, ""])
    parts.extend([f"**Score: {score}/100**", ""])
    parts.extend(_severity_table(issues))
    parts.extend(["", "### Issues found", ""])
    parts.extend(_grouped_issue_lines(issues))
    parts.extend(["---", _FOOTER])
    return "\n".join(parts)
