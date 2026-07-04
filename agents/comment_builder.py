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
    "architecture": "Architecture",
    "testing": "Test coverage",
}

_FOOTER = "*Reviewed by PR Sentinel — multi-agent review powered by Llama 3.3 (Groq)*"


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
    if issue.reviewer:
        agreement = "Multiple reviewers agree. " if "," in issue.reviewer else ""
        body += f"\n*{agreement}Flagged by: {issue.reviewer}*\n"
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


def _coverage_lines(failed_reviewers: list[str]) -> list[str]:
    """Render the warning block for reviewers that did not complete."""
    if not failed_reviewers:
        return []
    names = ", ".join(failed_reviewers)
    return [
        f"> ⚠️ **Partial review** — reviewer(s) did not complete: {names}. "
        "Findings may be incomplete.",
        "",
    ]


def _disagreement_lines(disagreements: list[dict[str, Any]]) -> list[str]:
    """List each same-line conflict with every reviewer's claim."""
    if not disagreements:
        return []
    lines = ["### Reviewers disagreed", ""]
    for record in disagreements:
        claims = "; ".join(
            f"{claim['reviewer']}: [{claim['severity'].upper()}] {claim['title']}"
            for claim in record.get("issues", [])
        )
        lines.append(f"- `{record['file']}:{record['line']}` — {claims}")
    lines.append("")
    return lines


def build_summary(
    issues: list[CodeIssue],
    score: int,
    overview: str,
    failed_reviewers: list[str] | None = None,
    disagreements: list[dict[str, Any]] | None = None,
) -> str:
    """Render the top-level review summary comment.

    Args:
        issues: The merged issues found by all reviewers.
        score: The 0-100 review score.
        overview: The AI-generated plain-English summary of the PR.
        failed_reviewers: Display names of reviewers that did not complete.
        disagreements: Same-line conflict records from agents.synthesis.
    """
    parts: list[str] = ["## PR Sentinel Review", ""]
    parts.extend(_coverage_lines(failed_reviewers or []))
    if overview:
        parts.extend([overview, ""])
    partial = " (partial)" if failed_reviewers else ""
    parts.extend([f"**Score: {score}/100{partial}**", ""])
    parts.extend(_severity_table(issues))
    parts.extend(["", "### Issues found", ""])
    parts.extend(_grouped_issue_lines(issues))
    parts.extend(_disagreement_lines(disagreements or []))
    parts.extend(["---", _FOOTER])
    return "\n".join(parts)
