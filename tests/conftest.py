"""Shared builders and fixtures for the PR Sentinel test suite.

Plain builder functions are imported directly from this module
(``from tests.conftest import make_issue``); realistic diff strings are
exposed as pytest fixtures. No test in the suite talks to a real API.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_client import CodeIssue


def make_issue(
    severity: str = "medium",
    file: str = "app.py",
    line: int = 10,
    category: str = "bug",
    title: str = "",
    explanation: str = "Something is wrong here.",
    suggestion: str = "Fix it like this.",
    reviewer: str = "",
) -> CodeIssue:
    """Build a CodeIssue with sensible defaults for tests."""
    return CodeIssue(
        file=file,
        line=line,
        severity=severity,
        category=category,
        title=title or f"{severity} {category} issue",
        explanation=explanation,
        suggestion=suggestion,
        reviewer=reviewer,
    )


def make_file(name: str = "app.py", changes: int = 3, patch: str = "+x = 1") -> dict[str, Any]:
    """Build one reviewable entry shaped like the GitHub PR files API."""
    return {"filename": name, "status": "modified", "patch": patch, "changes": changes}


def groq_response(content: str) -> SimpleNamespace:
    """Build a fake Groq chat completion wrapping the given content."""
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def groq_issues_response(issues: list[dict[str, Any]]) -> SimpleNamespace:
    """Build a fake Groq completion whose content is an issues JSON object."""
    return groq_response(json.dumps({"issues": issues}))


def fake_groq_client(
    response: Any = None, error: Exception | None = None
) -> MagicMock:
    """Build a stand-in AsyncGroq client returning a response or raising."""
    client = MagicMock()
    create = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
    client.chat.completions.create = create
    return client


@pytest.fixture()
def secret_diff() -> str:
    """Diff adding a hardcoded live API key to config_loader.py, line 12.

    The canonical disagreement surface: a style reviewer can honestly call
    line 12 a hardcoded literal while a security reviewer calls it a
    committed secret.
    """
    return (
        "@@ -8,4 +8,7 @@ def load_settings() -> dict:\n"
        "     settings = {}\n"
        "     with open(path) as fh:\n"
        "         settings.update(json.load(fh))\n"
        "+    # fall back to the production key when none is configured\n"
        '+    api_key = "sk-live-9f8e7d6c5b4a3210fedcba9876543210"\n'
        '+    settings.setdefault("api_key", api_key)\n'
        "     return settings"
    )


@pytest.fixture()
def sql_injection_diff() -> str:
    """Diff introducing string interpolation into a SQL query, line 21."""
    return (
        "@@ -20,3 +20,3 @@ def get_user(db, username):\n"
        '     """Fetch one user row by username."""\n'
        "-    query = \"SELECT * FROM users WHERE username = ?\"\n"
        "-    return db.execute(query, (username,)).fetchone()\n"
        "+    query = f\"SELECT * FROM users WHERE username = '{username}'\"\n"
        "+    return db.execute(query).fetchone()"
    )


@pytest.fixture()
def god_function_diff() -> str:
    """Diff adding a function that makes one HTTP call per loop iteration."""
    return (
        "@@ -0,0 +1,11 @@\n"
        "+import requests\n"
        "+\n"
        "+def sync_orders(order_ids, db):\n"
        '+    """Fetch and store every order, one HTTP call per order."""\n'
        "+    results = []\n"
        "+    for order_id in order_ids:\n"
        '+        response = requests.get(f"https://api.shop.com/orders/{order_id}")\n'
        "+        data = response.json()\n"
        '+        db.insert("orders", data)\n'
        "+        results.append(data)\n"
        "+    return results"
    )
