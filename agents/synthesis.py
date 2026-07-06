"""Deterministic synthesis of the focused reviewers' findings.

Merges the independent reviewers' outputs into one final issue list with
no LLM involvement, so every line number and severity in the posted
review is exactly what a reviewer reported. Overlapping findings collapse
with the highest severity winning; conflicting findings on the same line
are both kept and surfaced as disagreements. The only LLM step around
synthesis is the prose overview (llm_client.generate_synthesis_narrative),
which never edits the issue list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

import config
from agents.reviewers import ReviewerResult
from llm_client import CodeIssue

logger = logging.getLogger(__name__)


@dataclass
class SynthesisResult:
    """The merged outcome of all reviewers, ready for scoring and posting."""

    issues: list[CodeIssue] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    failed_reviewers: list[str] = field(default_factory=list)
    reviewer_status: dict[str, bool] = field(default_factory=dict)


def _severity_rank(severity: str) -> int:
    """Return the rank of a severity, higher meaning more severe."""
    ranks = tuple(reversed(config.SEVERITIES))
    try:
        return ranks.index(severity)
    except ValueError:
        return 0


def _contributor_names(issue: CodeIssue) -> set[str]:
    """Split an issue's reviewer field into its individual reviewer names."""
    return {name.strip() for name in issue.reviewer.split(",") if name.strip()}


def _merge_duplicates(issues: list[CodeIssue]) -> list[CodeIssue]:
    """Collapse same-(file, line, category) findings across reviewers.

    The highest-severity duplicate wins, and the surviving issue's reviewer
    field lists every reviewer that reported it, comma-joined and sorted —
    cross-reviewer agreement is a credibility signal worth showing. Input
    order is preserved for the surviving issues.
    """
    best: dict[tuple[str, int, str], CodeIssue] = {}
    contributors: dict[tuple[str, int, str], set[str]] = {}
    for issue in issues:
        key = (issue.file, issue.line, issue.category)
        contributors.setdefault(key, set()).update(_contributor_names(issue))
        current = best.get(key)
        if current is None or _severity_rank(issue.severity) > _severity_rank(
            current.severity
        ):
            best[key] = issue
    return [
        replace(issue, reviewer=", ".join(sorted(contributors[key])))
        for key, issue in best.items()
    ]


def _detect_disagreements(issues: list[CodeIssue]) -> list[dict[str, Any]]:
    """Find same-line findings where reviewers made conflicting claims.

    A disagreement is two or more distinct reviewers reporting the same
    (file, line) either with different categories, or with the same
    category but severities at least config.DISAGREEMENT_SEVERITY_GAP
    ranks apart. Records carry the original pre-merge findings so both
    claims stay visible even after the merge collapses one of them.
    """
    grouped: dict[tuple[str, int], list[CodeIssue]] = {}
    for issue in issues:
        grouped.setdefault((issue.file, issue.line), []).append(issue)
    records: list[dict[str, Any]] = []
    for (file, line), group in grouped.items():
        reviewers_involved = set().union(*(_contributor_names(i) for i in group))
        if len(reviewers_involved) < 2:
            continue
        categories = {issue.category for issue in group}
        ranks = [_severity_rank(issue.severity) for issue in group]
        gap = max(ranks) - min(ranks)
        if len(categories) > 1 or gap >= config.DISAGREEMENT_SEVERITY_GAP:
            records.append(
                {
                    "file": file,
                    "line": line,
                    "issues": [issue.to_dict() for issue in group],
                }
            )
    return records


def synthesize(results: list[ReviewerResult]) -> SynthesisResult:
    """Combine every reviewer's output into one deterministic result.

    Disagreements are detected on the raw findings (so conflicting claims
    are recorded before the merge collapses duplicates), then the issue
    list is merged for scoring and comment building. Failed reviewers'
    partial findings are included — disclosed, not discarded.
    """
    all_issues: list[CodeIssue] = []
    for result in results:
        all_issues.extend(result.issues)
    disagreements = _detect_disagreements(all_issues)
    merged = _merge_duplicates(all_issues)
    failed = [result.display_name for result in results if result.failed]
    status = {result.name: not result.failed for result in results}
    logger.info(
        "Synthesis: %d finding(s) merged to %d, %d disagreement(s), %d failed reviewer(s)",
        len(all_issues),
        len(merged),
        len(disagreements),
        len(failed),
    )
    return SynthesisResult(
        issues=merged,
        disagreements=disagreements,
        failed_reviewers=failed,
        reviewer_status=status,
    )
