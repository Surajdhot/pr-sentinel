"""Tests for config helpers and the reviewer registry. No real env reads."""

from __future__ import annotations

import pytest

import config


def test_env_csv_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset or blank variable falls back to the default tuple."""
    monkeypatch.delenv("TEST_CSV", raising=False)
    assert config._env_csv("TEST_CSV", allowed=("a", "b"), default=("a",)) == ("a",)
    monkeypatch.setenv("TEST_CSV", "   ")
    assert config._env_csv("TEST_CSV", allowed=("a", "b"), default=("a",)) == ("a",)


def test_env_csv_parses_and_strips_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated values are split, stripped and returned in order."""
    monkeypatch.setenv("TEST_CSV", " b , a ,")
    assert config._env_csv("TEST_CSV", allowed=("a", "b"), default=()) == ("b", "a")


def test_env_csv_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value outside the allowed set raises ConfigError naming it."""
    monkeypatch.setenv("TEST_CSV", "a,bogus")
    with pytest.raises(config.ConfigError, match="bogus"):
        config._env_csv("TEST_CSV", allowed=("a", "b"), default=())


def test_reviewer_registry_is_internally_consistent() -> None:
    """Every reviewer's categories and default exist in the global sets."""
    assert config.ENABLED_REVIEWERS  # never empty by default
    for spec in config.REVIEWERS:
        assert spec.default_category in spec.categories
        assert all(category in config.CATEGORIES for category in spec.categories)
        assert spec.name in config.REVIEWER_NAMES
