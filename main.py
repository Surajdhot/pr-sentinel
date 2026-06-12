"""CLI entrypoint for PR Sentinel.

Usage:
    python main.py review --repo owner/repo --pr 42
    python main.py review --repo owner/repo --pr 42 --dry-run
    python main.py serve
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

import uvicorn

import config
from agents import review_agent

logger = logging.getLogger("pr_sentinel")


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser with review and serve subcommands."""
    parser = argparse.ArgumentParser(
        prog="pr-sentinel",
        description="Autonomous AI code reviewer for GitHub pull requests.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="Review a pull request now")
    review.add_argument("--repo", required=True, help="Repository as owner/repo")
    review.add_argument("--pr", required=True, type=int, help="Pull request number")
    review.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and show findings in the terminal without posting to GitHub",
    )
    serve = sub.add_parser("serve", help="Start the webhook server")
    serve.add_argument("--host", default="0.0.0.0", help="Bind address")
    serve.add_argument("--port", default=8000, type=int, help="Bind port")
    return parser


def _parse_repo(value: str) -> tuple[str, str]:
    """Split an owner/repo string, exiting with a clear error if malformed."""
    owner, _, repo = value.partition("/")
    if not owner or not repo or "/" in repo:
        raise SystemExit(f"--repo must look like owner/repo, got: {value!r}")
    return owner, repo


def _log_findings(result: dict[str, Any]) -> None:
    """Write dry-run findings to the terminal via the logging module."""
    for issue in result["issues"]:
        logger.info(
            "[%s] %s:%d (%s) %s — %s",
            issue.severity.upper(),
            issue.file,
            issue.line,
            issue.category,
            issue.title,
            issue.explanation,
        )
    logger.info("Review summary:\n%s", result["summary"])


def run_review_command(args: argparse.Namespace) -> int:
    """Execute the review subcommand."""
    config.validate_config()
    owner, repo = _parse_repo(args.repo)
    result = asyncio.run(
        review_agent.run_review(owner, repo, args.pr, dry_run=args.dry_run)
    )
    if args.dry_run:
        _log_findings(result)
    logger.info(
        "Done: score %d/100 with %d issue(s)", result["score"], len(result["issues"])
    )
    return 0


def run_serve_command(args: argparse.Namespace) -> int:
    """Execute the serve subcommand."""
    config.validate_config(require_webhook_secret=True)
    uvicorn.run(
        "webhook:app",
        host=args.host,
        port=args.port,
        log_level=config.LOG_LEVEL.lower(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested command."""
    config.setup_logging()
    args = build_parser().parse_args(argv)
    if args.command == "review":
        return run_review_command(args)
    return run_serve_command(args)


if __name__ == "__main__":
    sys.exit(main())
