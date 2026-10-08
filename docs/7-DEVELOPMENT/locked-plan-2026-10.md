# Locked Plan — 2026-10 stabilization & hardening

Status · Date · Follows: ADR-009 (low-end x86 optimization), perf-audit.md

<!-- Status: LOCKED via brainstorm (2026-10) — every thread below was negotiated to a decision and locked before the next thread opened.
     Update status per thread as implementation lands. -->

## Implementation status (2026-10-08)

All locked threads for this cycle have landed on `main`. Full before/after
evidence for the measured items lives in `docs/7-DEVELOPMENT/perf-audit.md`.

| Thread | Commit(s) | Note |
|---|---|---|
| A1 · `search.vector` deficit | `262f226` (step 1: HNSW + KNN rewrite), `6592c59` (migration 29 seal) | Step 1 landed, then rejected/sealed after parity review — see perf-audit §A1; steps 2–5 stayed behind their locked gates (flags remain OFF) |
| A2 · Measurement close-out | `7e5239b` | RSS / podcast-baseline / cold-starts / chat+ollama / fixture-corpus recorded; #4 doc hygiene folded into the thread records |
| B1 · `run_coro_sync` helper | `b1d403e` | Contain, don't refactor — 3 call sites converted |
| B2 · retry visibility | `3f16eae` | Backoff+jitter already present; added retry-distribution log line + every-5th visibility milestone |
| B3 · job lifecycle | `a4fa072` | Progress + cooperative cancel + stale watchdog; +55 ms throttled accepted (stuck-command fix, not a speedup) |
| D1 · ntfy transport | `d8848b6` | Consumes B3 lifecycle events; no `started` event; fire-and-forget |
| B4 · auth guard | `806ba8c` | Opt-in `OPEN_NOTEBOOK_REQUIRE_AUTH`, default OFF, fail-fast |
| C1 · lockfile gate | `5800ac3` | CI job (`uv lock --check` + `uv sync --frozen`) + pre-commit mirror |
| C2 · flag defaults + offline boot | `ea49415` | 12-test deterministic pin; flags formalized in `.env.example`; manual offline-boot check documented + verified |

Parked / reassigned threads (D2–D5) remain exactly as locked below; nothing
here re-opens them.

The negotiated, locked work plan for the heavily modded open-notebook variant.
Every thread was brainstormed one at a time and locked before the next opened.
This document is the source of truth for *what* will be done, *in what order*,
and *why the parked items are parked*.

## Program constraints (non-negotiable, apply to every thread)

- All speedups/integrations **opt-in**; default feature flags stay **OFF**
  (mixture, reranker, hybrid, cloud).
- **One change at a time** for any perf work: throttled before/after probes via
  `scripts/throttle.sh` + `scripts/latency_probes.py`; keep ONLY strictly
  positive throttled deltas; rejected experiments get their rationale recorded
  here and in perf-audit.md.
- Offline-first; **never auto-enable cloud**. `CLOUD_ALLOWED` defaults false.
- `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` preserved.
- Full parity battery must stay green: pytest, ruff, mypy; vitest, eslint, next build.

---

## A1 · `search.vector` deficit (the flagged citizen)

Problem: `search.vector` is the one "BELOW BUDGET" citizen in the ADR-009
throttled tables (3363.7 ms p50 throttled vs 500 ms budget), while the scoped
text flow is 315 ms — the ~3 s delta is the vector machinery
(embed generation + vector index scan).

### Locked steps (run in this order; each is throttled before/after, opt-in, keep positive deltas only)

> **Order re-locked 2026-10-07 (twice).** ① **DB-first** by the decomposed probe
> (A1 step 0, perf-audit.md §Vector search decomposition): total 3813 ms =
> embed 217 + db 3401 (**~87%**) + assemble ~0, and the DB cost is weakly
> corpus-dependent (2395 ms on the 9-chunk fixture) → the DB-side attacks run
> *before* the embedding cache. ② **HNSW-first**: reading migration 27's
> `fn::vector_search` and timing its parts (perf-audit.md §A1 experiment-1
> design evidence) showed **no vector index exists** (only BM25 on `content` +
> plain `FIELDS source`) and the notebook scope is a *post-filter over a global
> brute-force cosine scan of all 3515 `source_embedding` chunks* — so
> "projection trim" alone is ~0-delta (the fn already returns a lean
> `{id,parent_id,title,similarity,matches}` and ≤15 candidate rows carry
> content) and the structural fix is the missing vector index. Step 1 is now
> the HNSW+KNN fn rewrite with lean projections folded in; projection-trim-alone
> is recorded as rejected-by-expected-zero-delta.

| Step | Change | Hypothesis / gate |
|---|---|---|
| 0 | **Decomposed probe flow** `search.vector.decomposed` writing `embed_ms / db_ms / assemble_ms` | **DONE (2026-10-07)** — whale is the DB-side lookup |
| 1 | **HNSW vector index + KNN rewrite of `fn::vector_search`**: `DEFINE INDEX ... HNSW DIMENSION 1024 DIST COSINE TYPE F32` on `source_embedding` / `source_insight` / `note` (all 1024-dim `array<float>`); fn rewritten to `<|fetch_k, EF|>` KNN with `source IN $scoped_sources` as KnnScan prefilter + cosine-threshold post-filter, lean projections, **identical output shape** (`id, parent_id, title, math::max(similarity), array::flatten(content) matches`). Projection trim folded in as the lean-projection requirement | Replaces the exact ~2.5–9 s global scan; recall/parity gate on the A2 fixture + 85-source corpus (top-K overlap + similarity drift) before latency wins count; expected DB phase ≪ 500 ms throttled |
| 2 | **Scoping indexes**: `DEFINE INDEX` on `reference.out` + `artifact.out` (scope subqueries ~0.3–0.7 s today, unindexed) | Cheaper scope resolution; measured separately so its delta stays attributable |
| 3 | **Payload trim**: content pulled only for grouped winners (KNN fetch-k carries content for fetch_k rows); list response ships ids+scores, chunk text lazy-fetched for LLM context | Only if KNN fetch-k shows a real content-transfer cost |
| 4 | Query-embedding cache (hash of normalized query → reuse embed) | Zero recall cost; caps the ~217 ms leg on repeated queries |
| 5 | Only if 1–4 exhausted — re-probe mixture / hybrid with fresh numbers | Flags stay OFF unless one proves positive; reranker already rejected (worse) and stays rejected |

### A1 non-goals

No flipping default flags ON; no embedder wholesale change; no SurrealDB
replacement; no ingest-semantics changes.

---

## A2 · Measurement debt close-out

Items the ADR-009 program promised but never finished. Pure-measurement items
are locked outright; the two forks were decided:

| # | Item | Locked decision |
|---|---|---|
| 1 | Worker + frontend RSS baselines (idle + task peak) | Extend RSS capture to whisper/piper/open_notebook-worker + node container; record in ADR-009 table |
| 2 | Podcast fast/draft preset under throttle | Record **baseline only** — no budget row exists for podcast, so no gate |
| 3 | Cold starts (worker <15 s, API throttled, frontend build) | Measure and record |
| 4 | perf-audit doc hygiene (stale "pending" headers, verdict cell) | Fold into docs pass **after** A1 step 0 + A2 measurements land (avoid churn) |
| 5 | Chat first-token budget (<2 s, "local models only") | **Add chat flow to harness; ollama wired INTO the throttled envelope.** Guardrail: timeout counts recorded separately from app latency so a starved-model timeout is never read as an app regression |
| 6 | Throttled heavy flows reset connection on 85-source corpus (2 GB envelope) | **Small numbered fixture notebook (3–5 sources, real chunks) becomes the throttled corpus for heavy flows.** 85-source corpus stays for unthrottled + memory-profile runs |

Rationale for #6 (recorded): the 85-source corpus exceeds what the 2 GB
envelope can serve for vector/global/ingest flows; using it for throttled
comparisons made before/after numbers incomparable. The fixture restores
repeatability without abandoning the big corpus for memory work.

---

## B · Debt & fragile spots

| # | Item | Locked decision |
|---|---|---|
| B1 | `asyncio.new_event_loop()` / ThreadPool sync-in-async pattern (AGENTS.md calls it fragile); **3 call sites**: `chat.py:41`, `source_chat.py:79`, `source_chat.py:155` | **Contain, don't refactor:** extract one `run_coro_sync` helper in `graph_utils.py` (next to existing `asyncio.to_thread` helpers); convert all 3 call sites; add unit test for the helper. Full async-first rewrite is deferred (high-risk, high-touch, no measurable user delta during stabilization) |
| B2 | `source_commands.py:48` `max_attempts: 15` workaround for SurrealDB v2 transaction conflicts; varied per-command retries exist (site=3, embedding=5, podcast=1, source=15 + source=5) | Keep the workaround; add **backoff + jitter** between attempts and a **retry-distribution log line** so a deep-queue stall is visible. Root-cause dig on SurrealDB v2 conflicts is parked (own thread) |
| B3 | Stuck-command UX gap: 42-page OCR ran >1 h "running" with zero progress, cleared only by restart | **Job-lifecycle pass, all three sub-fixes as one coherent change** (they share fields/endpoints): ① page-level progress increments surfaced through the existing progress path (notebook.py:488 already reads command progress); ② cooperative **cancel flag** checked between pages; ③ restart **watchdog** marks stale `running` commands as failed *with reason* (current behavior clears `command` to None = un-wedges the retry guard but loses the reason). **Post-change probe** re-runs ingest/OCR flows to prove heartbeat writes cost nothing |
| B4 | Auth short-circuit (D2 design): no password → API wide open on :5055; probes assume no auth | **Opt-in guard flag `OPEN_NOTEBOOK_REQUIRE_AUTH`, default off.** When on + no credentials configured → fail fast with a clear message. Preserves offline dev flow; kills the footgun for anyone who flips it. (Rejected: always-require-auth — breaks every tool in this repo; localhost-bind-only — composing; document-only — footgun stays) |

---

## C · Governance & offline-first

| # | Item | Locked decision |
|---|---|---|
| C1 | Broken-`uv.lock` incident (stub lock, hand-regenerated, 240 pkgs) must be structurally unrepeatable | **Lockfile gate:** `uv lock --check` + `uv sync --frozen` as a dedicated job in `.github/workflows/test.yml` AND mirrored as a pre-commit hook (repo convention: pre-commit mirrors CI gates — see `.pre-commit-config.yaml` header). Any future pyproject edit without lock regen fails locally + CI |
| C2 | "Never auto-enable cloud / flags stay OFF" enforced, not conventional | **Deterministic unit test** asserting `USE_MIXTURE`, `USE_RERANKER_BEFORE_TRUNCATE`, `CLOUD_ALLOWED` (and hybrid's flag once its exact name is pinned in `open_notebook/ai/model_policy.py`) are FALSE with env unset; formalize the flags + `CLOUD_ALLOWED=false` in `.env.example`; **one documented manual offline-boot check** (container starts with outbound network blocked). (Rejected: automated netns offline-boot CI test — netns tricks are unreliable/flaky on GH runners) |

---

## D · Integrations — ntfy only (this cycle)

### D1 · ntfy push notifications — LOCKED SPEC

| Element | Locked detail |
|---|---|
| Enable | `OPEN_NOTEBOOK_NOTIFY_URL` set to a topic (e.g. `https://ntfy.sh/boxy` or `http://ntfy:80/notebook`) — unset = fully disabled, **zero footprint** (no connection, no thread; HTTP POST exists only at event time). Optional `OPEN_NOTEBOOK_NOTIFY_TOKEN` → ntfy bearer auth. Document in `.env.example` |
| Events | Consumes B3's job-lifecycle events: **completed** and **failed** (with reason). No `started` event (noise on headless box) |
| Payload | Title `✓ <job> complete` / `✗ <job> failed`; body = notebook/source name + failure reason; `priority` 3, failures 4; `click` → notebook/source URL only when `OPEN_NOTEBOOK_BASE_URL` also set (else omit) |
| Delivery | Fire-and-forget async POST, 5 s timeout, **never affects job status**; single `warning` log on send failure; no retry queue |
| Placement | `open_notebook/utils/notify.py` (matches existing utils pattern); called from B3 lifecycle completion/failure sites. Unit tests mock httpx: send-failure + unreachable-URL are GREEN test cases; no network in tests |

Dependency: **B3 must land first** (D1 consumes B3's events).

### D2 · MCP — remains a documented NON-GOAL (revisit parked)

Expose-notebooks-to-MCP-clients (*expose-only*) was the only offline-compatible
direction; consuming MCP tools implies network = tension with the charter.
Revisit decision is explicitly parked; revisit requires a new, explicit
decision with the evaluation matrix (noted for a future cycle).

### D3 · Local ingestion (Obsidian vault watch / IMAP) — PARKED

Folder-watch is offline-clean; IMAP is opt-in network. Moderate build cost
(background poll loops). Parked with rationale; folder-watch first if ever
picked.

### D4 · Miniflux → auto-import sources — PARKED

Would reuse ADR-008 crawling + the in-stack Miniflux. High value for RSS-first
workflows, moderate cost. Parked after D1.

### D5 · STT engine swap (faster-whisper / Vosk) — REASSIGNED

Not an integration feature — a **measured perf experiment**. Reassigned to the
measurement threads (one-change-at-a-time, throttled probes). Whisper.cpp +
piper/piper-ja remain the shipped STT/TTS.

---

## Dependency order (drives sequencing)

1. **B3 (job lifecycle) → D1** (D1 needs B3's completed/failed events)
2. **A1 step 0 (decomposed probe) → any A1 experiment** (1–5)
3. **A2 #6 fixture → throttled heavy-flow comparisons resume**
4. C1 lockfile gate, B1 helper, B2 retry visibility, B4 auth guard — independent, land anytime
5. A2 #1/#2/#3/#5 measurement runs + #4 doc hygiene after the above land

## Reaffirmed non-goals

No default feature flag ON · no cloud auto-enable · OCR stays opt-in ·
no full `chat.py` async rewrite · no MCP tool consumption ·
no SurrealDB replacement · no STT engine swap outside measured experiments ·
`OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` preserved.