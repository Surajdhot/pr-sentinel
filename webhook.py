"""FastAPI webhook server for PR Sentinel.

Receives GitHub webhook deliveries, validates the HMAC signature, and kicks
off reviews as background tasks for opened/synchronized pull requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import config
from agents import review_agent

logger = logging.getLogger(__name__)

_HANDLED_ACTIONS = frozenset({"opened", "synchronize"})

# Strong references so in-flight review tasks are never garbage collected.
_background_tasks: set[asyncio.Task[Any]] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration and set up logging when the server starts."""
    config.setup_logging()
    config.validate_config(require_webhook_secret=True)
    logger.info("PR Sentinel webhook server started")
    yield


app = FastAPI(title="PR Sentinel", lifespan=lifespan)


def verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """Check the X-Hub-Signature-256 HMAC against the webhook secret."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    secret = (config.GITHUB_WEBHOOK_SECRET or "").encode()
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _on_review_done(task: asyncio.Task[Any]) -> None:
    """Log the outcome of a finished background review task."""
    _background_tasks.discard(task)
    if task.cancelled():
        logger.warning("Background review task was cancelled")
        return
    error = task.exception()
    if error is not None:
        logger.error("Background review failed: %s", error)


def _schedule_review(body: dict[str, Any]) -> bool:
    """Start the PR review as a background task; return True if scheduled."""
    repo_full = body.get("repository", {}).get("full_name", "")
    pr_number = body.get("pull_request", {}).get("number") or body.get("number")
    owner, _, repo = repo_full.partition("/")
    if not owner or not repo or not pr_number:
        logger.error("Webhook payload missing repository or PR number")
        return False
    task = asyncio.create_task(review_agent.run_review(owner, repo, int(pr_number)))
    _background_tasks.add(task)
    task.add_done_callback(_on_review_done)
    logger.info("Scheduled review for %s#%s", repo_full, pr_number)
    return True


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> Any:
    """Receive a GitHub webhook delivery and trigger a review if relevant.

    Invalid signatures are rejected with 401. Valid pull_request events with
    action opened/synchronize return 200 immediately and run the review in
    the background; every other event type returns 200 silently.
    """
    payload = await request.body()
    if not verify_signature(payload, request.headers.get("X-Hub-Signature-256")):
        logger.warning("Rejected webhook delivery with invalid signature")
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})
    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON"})
    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return {"status": "ignored", "reason": f"event {event or 'unknown'} not handled"}
    action = body.get("action", "")
    if action not in _HANDLED_ACTIONS:
        return {"status": "ignored", "reason": f"action {action!r} not handled"}
    if not _schedule_review(body):
        return {"status": "ignored", "reason": "payload missing repository or PR"}
    return {"status": "accepted"}
