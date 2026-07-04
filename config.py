"""Central configuration for PR Sentinel.

Loads environment variables from .env via python-dotenv and exposes every
project constant. No other module reads the environment directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to a default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Environment variable {name} must be an integer, got {raw!r}"
        ) from exc


def _env_csv(
    name: str, allowed: tuple[str, ...], default: tuple[str, ...]
) -> tuple[str, ...]:
    """Read a comma-separated environment variable, validating each entry.

    Args:
        name: Environment variable name.
        allowed: The values entries are permitted to take.
        default: Value returned when the variable is unset or blank.

    Raises:
        ConfigError: If any entry is not in the allowed set.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise ConfigError(
            f"Environment variable {name} contains unknown values: "
            f"{', '.join(unknown)}. Allowed: {', '.join(allowed)}"
        )
    return values


# --- Credentials (presence checked by validate_config at startup) ---
GROQ_API_KEY: str | None = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET: str | None = os.getenv("GITHUB_WEBHOOK_SECRET")
TARGET_REPO: str | None = os.getenv("TARGET_REPO")

REQUIRED_ENV_VARS: tuple[str, ...] = ("GROQ_API_KEY", "GITHUB_TOKEN")

# --- Review limits ---
MAX_FILES_PER_PR: int = _env_int("MAX_FILES_PER_PR", 20)
MAX_LINES_PER_CHUNK: int = _env_int("MAX_LINES_PER_CHUNK", 500)
MAX_FILE_LINES: int = 10000

# --- Scoring ---
SEVERITY_WEIGHTS: dict[str, int] = {"critical": 25, "high": 10, "medium": 5, "low": 1}
SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
CATEGORIES: tuple[str, ...] = (
    "bug",
    "security",
    "performance",
    "style",
    "error_handling",
    "architecture",
    "testing",
)


# --- Reviewers (multi-agent pipeline) ---
class ReviewerSpec(NamedTuple):
    """Definition of one focused reviewer in the multi-agent pipeline."""

    name: str
    display_name: str
    system_prompt: str
    categories: tuple[str, ...]
    default_category: str


REVIEWERS: tuple[ReviewerSpec, ...] = (
    ReviewerSpec(
        name="security",
        display_name="Security",
        system_prompt="security_system",
        categories=("security", "bug", "error_handling"),
        default_category="security",
    ),
    ReviewerSpec(
        name="style",
        display_name="Style & Quality",
        system_prompt="style_system",
        categories=("style", "testing", "error_handling"),
        default_category="style",
    ),
    ReviewerSpec(
        name="architecture",
        display_name="Architecture",
        system_prompt="architecture_system",
        categories=("architecture", "performance", "bug"),
        default_category="architecture",
    ),
)

REVIEWER_NAMES: tuple[str, ...] = tuple(spec.name for spec in REVIEWERS)
ENABLED_REVIEWERS: tuple[str, ...] = _env_csv(
    "ENABLED_REVIEWERS", allowed=REVIEWER_NAMES, default=REVIEWER_NAMES
)

# Severity-rank distance at which two same-line findings count as a
# disagreement between reviewers rather than a duplicate.
DISAGREEMENT_SEVERITY_GAP: int = 2

# --- LLM (Groq) ---
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
LLM_MAX_TOKENS: int = 4096
LLM_TEMPERATURE: float = 0.2
LLM_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0, 8.0)

# --- GitHub API ---
GITHUB_API_BASE: str = "https://api.github.com"
GITHUB_API_VERSION: str = "2022-11-28"
GITHUB_TIMEOUT_SECONDS: float = 30.0
GITHUB_RATE_LIMIT_RETRIES: int = 3
GITHUB_RATE_LIMIT_BASE_DELAY: float = 2.0

# --- Paths ---
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# --- Language detection for prompts and code fences ---
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "bash",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}


def validate_config(require_webhook_secret: bool = False) -> None:
    """Ensure required environment variables are present at startup.

    Args:
        require_webhook_secret: When True (webhook server mode), also require
            GITHUB_WEBHOOK_SECRET.

    Raises:
        ConfigError: If any required variable is missing, naming every
            missing variable.
    """
    required = list(REQUIRED_ENV_VARS)
    if require_webhook_secret:
        required.append("GITHUB_WEBHOOK_SECRET")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )


def setup_logging() -> None:
    """Configure root logging to stdout using LOG_LEVEL and LOG_FORMAT."""
    logging.basicConfig(
        level=LOG_LEVEL, format=LOG_FORMAT, stream=sys.stdout, force=True
    )
