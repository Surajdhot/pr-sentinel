"""Tests for llm_client retry and refusal handling. The SDK is fully mocked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import llm_client
from llm_client import ReviewFailedError


def _fake_client(message: Any) -> MagicMock:
    """Build a stand-in AsyncAnthropic client returning a canned message."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=message)
    return client


@pytest.mark.asyncio
async def test_refusal_stop_reason_raises_review_failed(mocker: Any) -> None:
    """A refusal response fails the file instead of passing it silently.

    claude-fable-5 can decline a request with HTTP 200, stop_reason
    "refusal" and empty content; treating that as "no issues found" would
    let an unreviewed file through.
    """
    refusal = SimpleNamespace(stop_reason="refusal", content=[])
    fake = _fake_client(refusal)
    mocker.patch("llm_client._get_client", return_value=fake)
    with pytest.raises(ReviewFailedError, match="refused"):
        await llm_client._create_with_retry(
            "app.py", model="test-model", max_tokens=16, messages=[]
        )
    fake.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_response_is_returned_without_retry(mocker: Any) -> None:
    """A normal end_turn response is returned from the first attempt."""
    message = SimpleNamespace(stop_reason="end_turn", content=[])
    fake = _fake_client(message)
    mocker.patch("llm_client._get_client", return_value=fake)
    result = await llm_client._create_with_retry(
        "app.py", model="test-model", max_tokens=16, messages=[]
    )
    assert result is message
    fake.messages.create.assert_awaited_once()
