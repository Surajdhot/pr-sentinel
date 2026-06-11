"""Central configuration for PR Sentinel.

Loads environment variables from .env via python-dotenv and exposes every
project constant. No other module reads the environment directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

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


# --- Credentials (presence checked by validate_config at startup) ---
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
GITHUB_TOKEN: str | None = os.getenv("GITHUB_TOKEN")
GITHUB_WEBHOOK_SECRET: str | None = os.getenv("GITHUB_WEBHOOK_SECRET")
TARGET_REPO: str | None = os.getenv("TARGET_REPO")

REQUIRED_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")

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
)

# --- LLM ---
# claude-sonnet-4-20250514 is deprecated by Anthropic (retires 2026-06-15);
# override with ANTHROPIC_MODEL=claude-sonnet-4-6 without a code change.
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
LLM_MAX_TOKENS: int = 4096
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
