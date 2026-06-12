# CLAUDE.md

## Project identity
PR Sentinel is an autonomous AI code reviewer. When a GitHub pull request
is opened, it fetches the diff, analyzes each changed file using Groq
(Llama 3.3) with structured JSON output, detects real bugs and security
issues, and posts inline review comments directly on the PR via the
GitHub API.

## Code style rules
- Python only. No JavaScript backend.
- All functions must have docstrings.
- Type hints on every function signature.
- Max function length: 40 lines. Split if longer.
- No print statements — use the logging module everywhere.
- Error handling: never use bare except. Always catch specific exceptions.
- Constants go in config.py, never hardcoded in logic files.
- Use async/await throughout — no synchronous HTTP calls.

## Architecture decisions — do not change these
- Agent logic lives in agents/ directory only.
- All GitHub API calls live in github_client.py only.
- All LLM calls live in llm_client.py only.
- No direct groq or httpx calls anywhere else in the codebase.
- All prompts live in prompts/ as .txt files — never hardcoded in Python.
- Configuration comes from .env via python-dotenv — no hardcoded secrets.

## What NOT to do
- Do not use LangChain — use the raw Groq Python SDK.
- Do not create any frontend — CLI and webhook only.
- Do not add unnecessary dependencies.
- Do not write placeholder, stub, or TODO code — everything must be
  fully functional.
- Do not commit .env — only .env.example gets committed.

## Testing
- Every module needs a corresponding test file in tests/.
- Use pytest with pytest-asyncio for async tests.
- Mock all external API calls in tests — no real API calls in test suite.

## Git commit style
- Use conventional commits: feat:, fix:, refactor:, test:, docs:, chore:
- One logical change per commit — never commit everything at once.
- Commit after each major component is working.
