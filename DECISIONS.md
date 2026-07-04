# Design decisions — multi-agent review pipeline

Decisions made autonomously while converting the single-pass review into
three focused reviewers plus a synthesis step. Recorded for post-hoc
review; each entry says what was decided, why, and what was rejected.

## D1. Provider: Groq (Llama 3.3), not the Anthropic API

The task description said each reviewer should have "its own Claude API
call", but the repository migrated from Anthropic to Groq at HEAD
(`5892762`) and CLAUDE.md mandates the raw Groq SDK with every LLM call
in `llm_client.py`. I read "Claude API call" as "its own independent LLM
call" and kept Groq. Adding an Anthropic dependency back one commit
after removing it seemed clearly unintended — if literal Claude API
calls were wanted, only `llm_client.py` and `config.py` need to change.

## D2. Reviewer abstraction: data-driven registry, one generic LLM function

Reviewers are `ReviewerSpec` entries in `config.REVIEWERS` (name, display
name, system-prompt file, allowed categories, default category). One
generic `llm_client.run_reviewer_analysis(spec, ...)` serves all of them;
orchestration lives in `agents/reviewers.py`.

**Why:** CLAUDE.md pins constants to `config.py` and LLM calls to
`llm_client.py`. Three near-identical functions would have duplicated
~90% of the request/parse code and tripled the test surface. Adding a
fourth reviewer is now one registry entry plus one prompt file.

**How to apply:** to add a reviewer, append a `ReviewerSpec`, create
`prompts/<name>_system.txt`, done. `ENABLED_REVIEWERS` (env, validated
against the registry) can disable reviewers for cost control or debugging.

## D3. Prompts: per-reviewer system prompts, shared analysis body

Each reviewer gets its own persona/focus/severity-calibration system
prompt (`security_system.txt`, `style_system.txt`,
`architecture_system.txt`). The user message (`reviewer_analysis.txt`) is
shared and parameterized with `{allowed_categories}`; the JSON schema is
identical for all reviewers so one parser serves everyone.

**Why:** the diff framing, chunking rules and output schema are
reviewer-independent — duplicating them three times guarantees drift. The
system prompt is where the reviewers genuinely differ. Prompts also
cross-reference ownership ("leave secrets to the security reviewer") to
reduce noise, though overlap is still expected and handled by synthesis.

## D4. Concurrency: gather at the reviewer level, sequential within

`run_all_reviewers` runs the three reviewers via `asyncio.gather`; each
reviewer walks its files/chunks sequentially, so at most three LLM
requests are in flight.

**Why:** total call volume tripled (3 × files × chunks; 60+ calls at the
20-file cap). Groq tiers are RPM/TPM-limited — per-chunk fan-out would
burst past limits and burn the retry budget. Reviewer-level gather still
gives ~3× wall-clock speedup and maps 1:1 to the failure-isolation
boundary. Escape hatch if TPM limits still bite: a semaphore inside
`_create_with_retry` (documented, not built).

## D5. Synthesis: deterministic merge + LLM narrative (the key decision)

The final issue list is produced by pure Python in `agents/synthesis.py`;
the only LLM call in synthesis writes prose (the overview) and can never
edit the issue list.

Merge rules:
- **Agreement** — same (file, line, category) from several reviewers →
  one issue, highest severity wins, all contributors credited in the
  `reviewer` field ("Multiple reviewers agree" shown on the comment).
- **Disagreement** — same (file, line) from ≥2 distinct reviewers with
  different categories, or same category with a severity gap ≥ 2 ranks
  (`DISAGREEMENT_SEVERITY_GAP`) → **both claims are kept**, a
  disagreement record is emitted (detected pre-merge so both original
  claims stay visible), the summary gets a "Reviewers disagreed" section,
  and the narrative call receives the disagreement JSON to explain it.

**Why not LLM-adjudicated merging:** a model that rewrites the issue list
can hallucinate line numbers — GitHub 422s on out-of-diff lines, and the
existing fallback would then silently drop *all* inline comments. It also
destroys traceability (which reviewer found what) and adds an
unbounded-context call. **Why not pure-deterministic:** it cannot produce
the coherent narrative or explain conflicts in prose.

**Judgment call within D5:** a security-critical claim is never averaged
away or suppressed by a style-low claim on the same line — both post, and
severity only ever merges upward. "Disagreement" is deliberately
conservative: different-category same-line findings are surfaced even
when they are complementary rather than contradictory, because showing
two expert readings side by side is cheap and hiding one is not.

## D6. Partial failure: degrade gracefully, disclose loudly

Each reviewer runs isolated (`ReviewerResult.failed`); partial findings
collected before a failure are kept. If ≥1 reviewer succeeds, the review
posts with a "Partial review" warning and the score labeled "(partial)".
Only when **all** reviewers fail does the bot post a PR-level failure
notice instead of a review (never in dry-run; CLI exits 1).

**Why:** a completed security pass is still valuable when the style pass
hit rate limits; discarding two-thirds of successful work to report
nothing is strictly worse. **Note:** this deliberately softens commit
`fbc1049` ("fail review on model refusal instead of passing silently") —
the spirit is preserved (failure is always disclosed, never silent) but
one reviewer's failure is no longer fatal. The per-file failure notices
were removed because three reviewers could post up to three notices per
file; failure reporting now happens once, at review level.

## D7. Scoring: unchanged formula on the post-merge set

`calculate_score` (100 − severity weights, floor 0) applies to the merged
issues. Agreement between reviewers does not change weights.

**Why:** overlap mostly reflects deliberate category overlap in the
prompts, not independent confirmation; weighting by agreement would
double-penalize exactly the duplicates the merge collapses, and would
make scores incomparable with historical reviews.

## D8. Old single-pass path: removed, not kept as fallback

`llm_client.analyze_file`, `llm_client.generate_summary`,
`agents/file_analyzer.analyze`, `prompts/file_analysis.txt` and
`prompts/summary.txt` are gone; `file_analyzer` keeps the reviewer-
agnostic skip rules and chunker. Git history is the fallback.

**Why:** a parallel legacy path doubles prompts, parser variants and the
test matrix for a bot with no external API consumers.

## D9. New categories and CodeIssue extension

`CATEGORIES` gained `architecture` and `testing`; `CodeIssue` gained a
defaulted `reviewer` field (backward compatible — existing construction
sites unaffected). Category coercion for a reviewer falls back to that
reviewer's default category, not the global "bug" — a style reviewer's
malformed category becoming "bug" would misweight the finding's meaning.

## Commit-sequence deviation from the original plan

The removals originally planned for commits 3–5 (old prompts and LLM
functions) were deferred into commit 6, because `review_agent` still
called them until the orchestrator flipped — removing them earlier would
have broken the suite mid-sequence. Every commit leaves the tests green.
