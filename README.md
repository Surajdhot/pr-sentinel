# PR Sentinel

PR Sentinel is an autonomous AI code reviewer for GitHub. When a pull request
is opened or updated, three focused reviewers — security, style & quality,
and architecture — independently analyze the diff with Groq's Llama 3.3
(structured JSON output), a synthesis step merges their findings into one
coherent review, and the result is posted as a scored GitHub review with
inline comments.

## How it works

```
GitHub webhook ──▶ webhook.py ──▶ agents/review_agent.py
                                       │
                          agents/reviewers.py (concurrent)
                        ┌──────────────┼────────────────┐
                        ▼              ▼                ▼
                   security       style/quality    architecture
                   reviewer         reviewer         reviewer
                        │              │                │
                        └──── llm_client (Groq) ────────┘
                                       │
                            agents/synthesis.py
                        (deterministic merge + conflicts)
                                       │
                          agents/comment_builder.py
                                       │
                              github_client.py
                                (post review)
```

1. A `pull_request` webhook (or the CLI) triggers a review.
2. Changed files are fetched; deleted, binary, and >10,000-line files are
   skipped, and only the 20 largest files are reviewed on huge PRs.
3. Each enabled reviewer independently analyzes every file's diff (split
   into ≤500-line chunks at function/class boundaries) with its own system
   prompt and category focus. Reviewers run concurrently; each returns
   findings as JSON.
4. Synthesis merges the findings deterministically: identical findings
   collapse (highest severity wins, all reviewers credited); conflicting
   same-line findings are both kept and listed under "Reviewers disagreed".
   One LLM call writes the prose overview — it never edits the issue list.
5. The merged review is scored, formatted, and posted with inline comments,
   a summary table, and reviewer attribution. If a reviewer fails, the
   review posts anyway marked "partial"; only if all fail does the bot post
   a failure notice instead.

**Scoring:** `100 − (critical×25 + high×10 + medium×5 + low×1)`, floored at 0.

Design rationale for the pipeline lives in [DECISIONS.md](DECISIONS.md).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `GROQ_API_KEY` | yes | Groq API key (free at console.groq.com) |
| `GITHUB_TOKEN` | yes | GitHub PAT with `repo` / PR read-write access |
| `GITHUB_WEBHOOK_SECRET` | for `serve` | Validates `X-Hub-Signature-256` on deliveries |
| `TARGET_REPO` | no | Default repo hint (`owner/repo`) |
| `MAX_FILES_PER_PR` | no | Max files reviewed per PR (default 20) |
| `MAX_LINES_PER_CHUNK` | no | Max diff lines per LLM call (default 500) |
| `ENABLED_REVIEWERS` | no | Comma list of reviewers to run (default `security,style,architecture`) |
| `GROQ_MODEL` | no | Groq model used for reviews (default `llama-3.3-70b-versatile`) |
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

All external API calls (GitHub and Groq) are mocked — the suite makes no
network requests.

## Project conventions

See [CLAUDE.md](CLAUDE.md) for the code style and architecture rules this
repository follows.
