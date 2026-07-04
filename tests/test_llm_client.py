"""Tests for llm_client Groq integration and JSON parsing. SDK fully mocked."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import groq
import httpx
import pytest

import config
import llm_client
from llm_client import ReviewFailedError
from tests.conftest import fake_groq_client, groq_issues_response, groq_response


def test_parse_issues_reads_valid_json() -> None:
    """A well-formed JSON object becomes CodeIssue objects."""
    content = (
        '{"issues": [{"file": "app.py", "line": 3, "severity": "high", '
        '"category": "security", "title": "SQL injection", '
        '"explanation": "User input is interpolated.", '
        '"suggestion": "Use parameters."}]}'
    )
    issues = llm_client._parse_issues(content, "app.py")
    assert len(issues) == 1
    assert issues[0].severity == "high"
    assert issues[0].category == "security"
    assert issues[0].line == 3


def test_parse_issues_handles_empty_and_malformed() -> None:
    """Empty arrays yield no issues; unparseable content yields none."""
    assert llm_client._parse_issues('{"issues": []}', "app.py") == []
    assert llm_client._parse_issues("not json at all", "app.py") == []


def test_parse_issues_coerces_unknown_enums() -> None:
    """Out-of-range severity/category fall back to safe defaults."""
    content = (
        '{"issues": [{"file": "app.py", "line": 1, "severity": "scary", '
        '"category": "vibes", "title": "t", "explanation": "e", "suggestion": "s"}]}'
    )
    issues = llm_client._parse_issues(content, "app.py")
    assert issues[0].severity == "medium"
    assert issues[0].category == "bug"


def test_parse_issues_skips_malformed_entries() -> None:
    """An issue missing required fields is dropped, valid ones are kept."""
    content = (
        '{"issues": [{"file": "app.py", "severity": "low"}, '
        '{"file": "app.py", "line": 5, "severity": "low", "category": "style", '
        '"title": "t", "explanation": "e", "suggestion": "s"}]}'
    )
    issues = llm_client._parse_issues(content, "app.py")
    assert len(issues) == 1
    assert issues[0].line == 5


@pytest.mark.asyncio
async def test_analyze_file_returns_parsed_issues(mocker: Any) -> None:
    """analyze_file sends the diff to Groq in JSON mode and parses the result."""
    content = (
        '{"issues": [{"file": "app.py", "line": 9, "severity": "medium", '
        '"category": "bug", "title": "Bug", "explanation": "x", "suggestion": "y"}]}'
    )
    fake = fake_groq_client(groq_response(content))
    mocker.patch("llm_client._get_client", return_value=fake)
    issues = await llm_client.analyze_file("app.py", "@@ -1 +1 @@\n+x = 1")
    assert len(issues) == 1
    fake.chat.completions.create.assert_awaited_once()
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == llm_client.config.GROQ_MODEL


@pytest.mark.asyncio
async def test_api_error_raises_review_failed(mocker: Any) -> None:
    """Repeated API errors raise ReviewFailedError after exhausting retries."""
    err = groq.APIConnectionError(
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    fake = fake_groq_client(error=err)
    mocker.patch("llm_client._get_client", return_value=fake)
    mocker.patch("llm_client.asyncio.sleep", AsyncMock())  # skip backoff delays
    with pytest.raises(ReviewFailedError):
        await llm_client.analyze_file("app.py", "@@ -1 +1 @@\n+x = 1")
    assert fake.chat.completions.create.await_count == 1 + len(
        llm_client.config.LLM_RETRY_DELAYS
    )


def _issue_dict(spec: config.ReviewerSpec, line: int = 9) -> dict[str, Any]:
    """Build one raw model-issue dict valid for the given reviewer."""
    return {
        "file": "app.py",
        "line": line,
        "severity": "medium",
        "category": spec.default_category,
        "title": "Finding",
        "explanation": "x",
        "suggestion": "y",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", config.REVIEWERS, ids=lambda s: s.name)
async def test_run_reviewer_analysis_uses_reviewer_prompt(
    mocker: Any, spec: config.ReviewerSpec
) -> None:
    """Each reviewer sends its own system prompt and category whitelist."""
    fake = fake_groq_client(groq_issues_response([_issue_dict(spec)]))
    mocker.patch("llm_client._get_client", return_value=fake)
    issues = await llm_client.run_reviewer_analysis(
        spec, "app.py", "@@ -1 +1 @@\n+x = 1"
    )
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == config.GROQ_MODEL
    assert kwargs["messages"][0]["content"] == llm_client._load_prompt(
        spec.system_prompt
    )
    assert ", ".join(spec.categories) in kwargs["messages"][1]["content"]
    assert len(issues) == 1
    assert issues[0].reviewer == spec.name


@pytest.mark.asyncio
async def test_reviewer_category_falls_back_to_reviewer_default(mocker: Any) -> None:
    """An off-list category becomes the reviewer's default, not 'bug'."""
    spec = next(s for s in config.REVIEWERS if s.name == "security")
    raw = _issue_dict(spec) | {"category": "vibes"}
    fake = fake_groq_client(groq_issues_response([raw]))
    mocker.patch("llm_client._get_client", return_value=fake)
    issues = await llm_client.run_reviewer_analysis(spec, "app.py", "+x = 1")
    assert issues[0].category == "security"


@pytest.mark.asyncio
async def test_reviewer_analysis_retries_then_fails(mocker: Any) -> None:
    """run_reviewer_analysis exhausts retries and raises ReviewFailedError."""
    err = groq.APIConnectionError(
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    )
    fake = fake_groq_client(error=err)
    mocker.patch("llm_client._get_client", return_value=fake)
    mocker.patch("llm_client.asyncio.sleep", AsyncMock())  # skip backoff delays
    with pytest.raises(ReviewFailedError):
        await llm_client.run_reviewer_analysis(
            config.REVIEWERS[0], "app.py", "+x = 1"
        )
    assert fake.chat.completions.create.await_count == 1 + len(
        config.LLM_RETRY_DELAYS
    )


@pytest.mark.asyncio
async def test_generate_summary_returns_text(mocker: Any) -> None:
    """generate_summary returns the model's plain-text content."""
    fake = fake_groq_client(groq_response("This PR adds a feature. Most important: none."))
    mocker.patch("llm_client._get_client", return_value=fake)
    text = await llm_client.generate_summary([], {"title": "Add feature"})
    assert "This PR adds a feature." in text
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs  # summary is free-form text, not JSON


@pytest.mark.asyncio
async def test_synthesis_narrative_is_free_form_and_fully_templated(
    mocker: Any,
) -> None:
    """The narrative call sends prose context, not JSON mode, to the model."""
    fake = fake_groq_client(groq_response("Solid PR. Most important: rotate the key."))
    mocker.patch("llm_client._get_client", return_value=fake)
    issue = llm_client.CodeIssue(
        file="config_loader.py",
        line=12,
        severity="critical",
        category="security",
        title="Live API key committed",
        explanation="x",
        suggestion="y",
        reviewer="security",
    )
    disagreements = [{"file": "config_loader.py", "line": 12, "issues": []}]
    text = await llm_client.generate_synthesis_narrative(
        [issue],
        disagreements,
        {"security": True, "style": False},
        {"title": "Add config loader", "body": None, "changed_files": 1},
    )
    assert text == "Solid PR. Most important: rotate the key."
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs  # narrative is free-form text
    user_message = kwargs["messages"][1]["content"]
    assert "security: completed, style: FAILED" in user_message
    assert "Live API key committed" in user_message
    assert '"line": 12' in user_message
    assert "(no description)" in user_message
    assert "{pr_title}" not in user_message  # every placeholder was filled
    assert "{issues_json}" not in user_message
