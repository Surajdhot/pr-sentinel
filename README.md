# PR Sentinel

PR Sentinel is an autonomous AI code reviewer for GitHub. When a pull request
is opened or updated, it fetches the diff, analyzes each changed file with
Claude (using tool use), detects real bugs and security issues, and posts a
scored review with inline comments directly on the PR.

## How it works

```
GitHub webhook ──▶ webhook.py ──▶ agents/review_agent.py
                                       │
                        ┌──────────────┼────────────────┐
                        ▼              ▼                ▼
              agents/file_analyzer  llm_client    agents/comment_builder
              (smart diff chunking) (Claude +     (GitHub review format)
                        │            tool use)          │
                        └──────▶ github_client.py ◀─────┘
                                 (post review)
```

1. A `pull_request` webhook (or the CLI) triggers a review.
2. Changed files are fetched; deleted, binary, and >10,000-line files are
   skipped, and only the 20 largest files are reviewed on huge PRs.
3. Each file's diff is split into ≤500-line chunks at function/class
   boundaries (falling back to blank lines) and sent to Claude, which
   reports findings by calling the `report_code_issue` tool.
4. Findings are deduplicated (same file + line + category keeps the higher
   severity), scored, formatted, and posted as a GitHub review with inline
   comments and a summary table.

**Scoring:** `100 − (critical×25 + high×10 + medium×5 + low×1)`, floored at 0.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | yes | Claude API key |
| `GITHUB_TOKEN` | yes | GitHub PAT with `repo` / PR read-write access |
| `GITHUB_WEBHOOK_SECRET` | for `serve` | Validates `X-Hub-Signature-256` on deliveries |
| `TARGET_REPO` | no | Default repo hint (`owner/repo`) |
| `MAX_FILES_PER_PR` | no | Max files reviewed per PR (default 20) |
| `MAX_LINES_PER_CHUNK` | no | Max diff lines per LLM call (default 500) |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-sonnet-4-20250514`. **That model is deprecated and retires 2026-06-15** — set `ANTHROPIC_MODEL=claude-sonnet-4-6` to upgrade without a code change. |
| `LOG_LEVEL` | no | Logging level (default `INFO`) |

## Usage

```bash
# Review a PR and post the review to GitHub
python main.py review --repo owner/repo --pr 42

# Analyze only — show findings in the terminal, post nothing
python main.py review --repo owner/repo --pr 42 --dry-run

# Start the webhook server (default 0.0.0.0:8000)
python main.py serve
```

### Webhook setup

1. Run the server somewhere GitHub can reach (or use a tunnel like ngrok).
2. In your repo: **Settings → Webhooks → Add webhook**
   - Payload URL: `https://your-host/webhook`
   - Content type: `application/json`
   - Secret: the value of `GITHUB_WEBHOOK_SECRET`
   - Events: *Pull requests*
3. Deliveries with invalid signatures are rejected with 401. Reviews run as
   background tasks; the webhook returns 200 immediately.
   `GET /health` returns `{"status": "ok"}` for liveness probes.

### Docker

```bash
cp .env.example .env   # fill in your keys
docker compose up --build
```

## Testing

```bash
pytest tests/
```

All external API calls (GitHub and Anthropic) are mocked — the suite makes
no network requests.

## Project conventions

See [CLAUDE.md](CLAUDE.md) for the code style and architecture rules this
repository follows.
