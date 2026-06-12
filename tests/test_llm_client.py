"""Tests for llm_client Groq integration and JSON parsing. SDK fully mocked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import groq
import httpx
import pytest

import llm_client
from llm_client import ReviewFailedError


def _response(content: str) -> SimpleNamespace:
    """Build a fake Groq chat completion wrapping the given content."""
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _fake_client(response: Any = None, error: Exception | None = None) -> MagicMock:
    """Build a stand-in AsyncGroq client returning a response or raising."""
    client = MagicMock()
    create = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
    client.chat.completions.create = create
    return client


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
    fake = _fake_client(_response(content))
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
    fake = _fake_client(error=err)
    mocker.patch("llm_client._get_client", return_value=fake)
    mocker.patch("llm_client.asyncio.sleep", AsyncMock())  # skip backoff delays
    with pytest.raises(ReviewFailedError):
        await llm_client.analyze_file("app.py", "@@ -1 +1 @@\n+x = 1")
    assert fake.chat.completions.create.await_count == 1 + len(
        llm_client.config.LLM_RETRY_DELAYS
    )


@pytest.mark.asyncio
async def test_generate_summary_returns_text(mocker: Any) -> None:
    """generate_summary returns the model's plain-text content."""
    fake = _fake_client(_response("This PR adds a feature. Most important: none."))
    mocker.patch("llm_client._get_client", return_value=fake)
    text = await llm_client.generate_summary([], {"title": "Add feature"})
    assert "This PR adds a feature." in text
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs  # summary is free-form text, not JSON
