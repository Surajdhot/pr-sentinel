"""Tests for agents.synthesis — the deterministic merge, no I/O involved."""

from __future__ import annotations

from agents import synthesis
from agents.review_agent import calculate_score
from agents.reviewers import ReviewerResult
from tests.conftest import disagreement_results, make_issue


def _result(name: str, issues: list, failed: bool = False) -> ReviewerResult:
    """Build a ReviewerResult with the display name derived from the name."""
    return ReviewerResult(
        name=name, display_name=name.title(), issues=issues, failed=failed
    )


def test_agreement_collapses_to_highest_severity() -> None:
    """Two reviewers reporting the same finding merge into one issue."""
    security = make_issue("high", line=10, category="bug", reviewer="security")
    architecture = make_issue("medium", line=10, category="bug", reviewer="architecture")
    synth = synthesis.synthesize(
        [_result("security", [security]), _result("architecture", [architecture])]
    )
    assert len(synth.issues) == 1
    assert synth.issues[0].severity == "high"
    assert synth.issues[0].reviewer == "architecture, security"
    assert synth.disagreements == []  # same category, gap of 1 rank


def test_disagreement_keeps_both_claims_and_records_it() -> None:
    """The canonical secret-vs-style conflict survives synthesis intact."""
    synth = synthesis.synthesize(disagreement_results())
    assert len(synth.issues) == 2
    severities = {issue.category: issue.severity for issue in synth.issues}
    assert severities == {"security": "critical", "style": "low"}
    assert len(synth.disagreements) == 1
    record = synth.disagreements[0]
    assert record["file"] == "config_loader.py"
    assert record["line"] == 12
    assert len(record["issues"]) == 2
    # each claim penalised exactly once: 100 - (25 + 1)
    assert calculate_score(synth.issues) == 74


def test_severity_gap_same_category_merges_but_records_disagreement() -> None:
    """Same category, critical vs low: merged to critical, still recorded."""
    high_claim = make_issue("critical", line=5, category="bug", reviewer="security")
    low_claim = make_issue("low", line=5, category="bug", reviewer="architecture")
    synth = synthesis.synthesize(
        [_result("security", [high_claim]), _result("architecture", [low_claim])]
    )
    assert len(synth.issues) == 1
    assert synth.issues[0].severity == "critical"
    assert synth.issues[0].reviewer == "architecture, security"
    assert len(synth.disagreements) == 1
    assert synth.disagreements[0]["line"] == 5


def test_one_reviewer_alone_is_never_a_disagreement() -> None:
    """Two same-line findings from one reviewer are not a conflict."""
    issues = [
        make_issue("high", line=7, category="security", reviewer="security"),
        make_issue("low", line=7, category="error_handling", reviewer="security"),
    ]
    synth = synthesis.synthesize([_result("security", issues)])
    assert len(synth.issues) == 2
    assert synth.disagreements == []


def test_failed_reviewer_bookkeeping_keeps_partial_findings() -> None:
    """A failed reviewer is reported and its partial issues still merge."""
    partial = make_issue("medium", line=3, reviewer="style")
    synth = synthesis.synthesize(
        [
            _result("security", [], failed=False),
            _result("style", [partial], failed=True),
        ]
    )
    assert synth.issues == [partial]
    assert synth.failed_reviewers == ["Style"]
    assert synth.reviewer_status == {"security": True, "style": False}


def test_distinct_findings_pass_through_in_order() -> None:
    """Non-overlapping findings survive untouched, keeping their reviewer."""
    first = make_issue("low", file="a.py", line=1, reviewer="style")
    second = make_issue("high", file="b.py", line=2, reviewer="security")
    synth = synthesis.synthesize(
        [_result("style", [first]), _result("security", [second])]
    )
    assert [issue.file for issue in synth.issues] == ["a.py", "b.py"]
    assert [issue.reviewer for issue in synth.issues] == ["style", "security"]
