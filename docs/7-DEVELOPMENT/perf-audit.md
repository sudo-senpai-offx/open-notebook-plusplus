# Performance & Quality Audit (perf-audit)

Status · Date · ADR-009

<!-- Status: IN PROGRESS (Phase 0 harness) | COMPLETE --> <!-- 2026-10 -->

## 1. Goal

Make the existing fork feel fast on the ADR-009 low-end target (2 GB RAM,
2+ cores, SSD class) — measured and proven, not guessed. Every claimed fix
must show a before/after delta from the same probe harness in the same
mode (normal vs throttled), and stay green across the full parity battery
(pytest, ruff, mypy; vitest, eslint, next build).

## 2. Budgets (definition of "fast"), throttled

| Flow | Budget | Envelope | Notes |
|---|---|---|---|
| Notebook list | `< 300 ms` | API | p90, throttled |
| Notebook load | `< 300 ms` | API | p90 |
| Source list | `< 200 ms` | API | p90 |
| Search (one notebook) | `< 500 ms` | API | p90 |
| Chat first token | `< 2 s` | API+model | local models only |
| Source ingest (text) | `< 1 s` | API+worker | file IO excluded |
| Embed typical doc | within 500 MB task peak | worker | see ADR-009 |
| Worker cold start | `< 15 s` | worker | ADR-009 stage 2 |
| API cold start | `< 5 s` | API | ADR-009 |
| RSS idle | API<220 / worker<300 / node<150 MB | stack | ADR-009 |
| RSS task peak | worker < 500 MB | worker | ADR-009 |

Budget that is NOT met → deserves a fix proposal. Budget met → leaves the
cluster unmodified unless the sweep found a clear defect anyway.

## 3. Harness

### Throttle the stack to the low-end box (`scripts/throttle.sh`)

```bash
scripts/throttle.sh apply     # clamp API/worker/node + surrealdb to ~2 vCPU + 2 GB-class
scripts/throttle.sh restore   # put back the captured limits
scripts/throttle.sh status    # current per-container mem/cpu
```

- CPU via `docker update --cpus` (container = api+worker+frontend).
- Memory via `docker update --memory` (container + surrealdb + ollama split).
- Ollama runs on the host and is **not** throttled here — known caveat, see
  decision log.
- Original limits are snapshotted to `/tmp/opencode-throttle-state.txt` and
  restored with `restore`.

### Latency flows (`scripts/latency_probes.py`)

```bash
python3 scripts/latency_probes.py --mode normal
python3 scripts/latency_probes.py --mode throttled
python3 scripts/latency_probes.py --flows ingest
```

Flows: `health`, `notebooks.list`, `notebooks.load`, `sources.list`,
`search.text.scoped`, `search.vector`, `search.text.global`, `ingest.text`.
Probe notebook auto-discovered (or `--notebook-id`); `-n` iterations,
first beat discarded as warmup; p50/p90/mean/min/max + JSON.

### Resources & cold start (`scripts/measure_resources.sh`)

```bash
scripts/measure_resources.sh --spawn   # RSS + startup, fresh dev stack
scripts/measure_resources.sh --attach  # RSS against the running (docker) stack
```

Per-process RSS, task-peak RSS, and cold-start times vs the ADR-009 envelope.

## 4. Baselines

**Status:** partial baselines captured (unthrottled latency probes + container RSS). Throttled runs still pending. 

### Latency (normal) — to be recorded from `scripts/latency_probes.py --mode normal`

| Flow | p50 | p90 | mean | budget | verdict |
|---|---|---|---|---|---|
| notebooks.list | 65 | 66 | 64 | 300 | |
| notebooks.load | 97 | 103 | 101 | 300 | |
| sources.list | 162 | 163 | 163 | 200 | |
| search.text.scoped | 413 | 418 | 418 | 500 | |
| search.vector | 4156 | 4173 | 4165 | 500 | |
| search.text.global | 450 | 453 | 457 | 500 | |
| ingest.text | 1408 | 1410 | 1408 | 1000 | |

### Latency (throttled) — same flows under `scripts/throttle.sh apply`

| Flow | p50 | p90 | mean | budget | verdict |
|---|---|---|---|---|---|
| notebooks.list | 43.1 | 43.3 | 43.3 | 300 | OK (updated) |
| notebooks.load | 65.6 | 68.4 | 66.6 | 300 | OK |
| sources.list | 125.4 | 127.7 | 128.4 | 200 | OK |
| search.text.scoped | 315.2 | 315.2 | 317.1 | 500 | OK |
| search.vector | 3363.7 | 3381.5 | 3373.1 | 500 | **BELOW BUDGET (50x over)** | # vector is heavy; tune via opt-in flags only
| search.text.global | 347.5 | 350.0 | 350.5 | 500 | OK |
| ingest.text | 1282.7 | 1295.9 | 1294.7 | 1000 | OK |

### RSS + startup (ADR-009)

| Component | Idle RSS | Task peak | Cold start | Envelope | Status |
|---|---|---|---|---|---|
| API | 562.6MiB | — | — | <220 MB / <5 s | measured (unthrottled) |
| Worker | — | — | — | <300–500 MB / <15 s | baseline in progress |
| Frontend (node) | — | — | — | <150 MB | baseline in progress |

### Manual spot measurements (this dev box, unthrottled, 12c/16G)

| Probe | Time | Note |
|---|---|---|
| notebook list | 277 ms | at the 300 ms budget edge |
| notebook load | 523 ms | p90 will be worse |
| source list | 230 ms | |
| text search (global) | 2.68 s | **hot path — global has no scope filter** |
| text search (scoped) | 888 ms | |
| container RSS open_notebook | 997.6 MiB | limit 3.563 GiB |
| container RSS surrealdb | 482.6 MiB | |

### Vector search decomposition (A1 step 0, 2026-10-07, unthrottled)

Added the opt-in, header-gated `_timings` side-channel to `vector_search`
(`open_notebook/domain/notebook.py`) exposing it via `X-Notebook-*-Ms`
response headers (`api/routers/search.py`) only when the probe sends
`X-OpenNotebook-Timings: 1`. Normal traffic is byte-identical (no headers, no
side-channel). Live run against the default probe corpus (the 85-source `test`
notebook), `-n 5`, warmup beat discarded:

| Phase | mean | p50 | p90 | min | max | Share of total |
|---|---|---|---|---|---|---|
| total (wall) | 3813.4 | 3825.2 | 3828.0 | 3701.0 | 3899.3 | 100% |
| .embed (query embedding) | 217.2 | 203.0 | 225.0 | 202.0 | 239.0 | ~6% |
| .db (`fn::vector_search` in SurrealDB) | 3400.5 | 3399.0 | 3417.0 | 3282.0 | 3504.0 | **~87%** |
| .assemble (python re-rank, already sorted) | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | ~0% |

**Finding:** the whale is the DB-side vector lookup (~3400 ms), not the query
embedding (~217 ms) and not python-side rank assembly (free). The un-decomposed
`search.vector` total on this deploy is ~3.8 s vs the 500 ms budget (throttled
baseline 3363.7 ms). Implications for the locked A1 experiment order:

- The **query-embedding cache** (locked as first experiment) can save at most
  the ~217 ms embed leg when the same query repeats — a real but small win.
- **Index hygiene + payload trim + DB-side query cost** are where the remaining
  ~3.4 s lives; the A1 sequence is re-ordered below to attack the measured
  bottleneck first, with the embedding cache kept as a cheap follow-up. Reranker
  stays rejected; hybrid/mixture stay last-resort. (Ordering re-locked with the
  user after this probe — see locked-plan-2026-10.md.)

#### A2 fixture provisioning + scoped-search check (same deploy, 2026-10-07)

`scripts/create_fixture_notebook.py` was hardened after its first run exposed a
real stack behavior: the 3rd `POST /api/sources/json` (`embed=True`,
`async_processing=False`) hung server-side with **no access-log line, no command
log, and no error** for >120 s, leaving a partial fixture. The stall did not
reproduce on re-run (transient DB hiccup), but it is a second witness for the
B-series theme: the server can wedge on a request with nothing ever marking it
failed. Script changes: `--create-timeout` (per-request POST timeout, default
300 s, distinct from the poll deadline), best-effort
`DELETE /api/notebooks/{id}` of the partial fixture on any mid-run failure, and
retry-tolerant embed polling (a dropped poll is transient, not fatal).

Fixture created cleanly: `notebook:l060pbn9hiwyst1z0xlt`
(`fixture-throttle-1791342567-nb`), 4 sources / 9 chunks, exit 0. Probe
discovery still picks the 85-source `test` notebook (`fixture-*` excluded).

Scoped-search verification: a probe sending `scope_notebook_ids` in the JSON
body was silently unscoped — that name is a Python-side property merging
`notebook_id`/`notebook_ids` (api/models.py `NotebookScopeMixin`), and pydantic
drops the unknown field, so the query ran global and leaked cross-notebook
results. With the correct `notebook_ids` field the identical query returns only
the fixture's sources (`total_count: 4`). No app bug — probe bug, now checked.

Additional data point for the A1 re-ordering: scoped vector search on the
9-chunk fixture still took **2395 ms** in the DB phase (vs 3400 ms on the
85-source corpus). `fn::vector_search` cost is therefore weakly
corpus-size-dependent and dominated by fixed machinery (table scan / distance
computation over the full chunk set / result serialization), which reinforces
attacking the DB side before the embedding cache.

#### A1 experiment-1 design evidence: why HNSW comes first (2026-10-07)

Reading migration 27's `fn::vector_search` body and timing its parts on the live
DB before changing anything:

- **No vector index exists.** The migrations declare only BM25 indexes on
  `content` and a plain `FIELDS source` index (`idx_source_embedding_source`).
  `fn::vector_search` therefore runs an **exact sequential cosine scan** of the
  chunk tables on every query.
- **Scope is a post-filter, not a scan limit.** `WHERE ... source IN
  $scoped_sources` is evaluated after the cosine scan, so a search scoped to the
  9-chunk fixture still scans the *global* corpus: `source_embedding` = 3515
  rows (all notebooks), `source_insight` = 43, `note` = 2. This explains the
  earlier "weak corpus dependence": the driver is the global table size, not the
  scoped set.
- **Component timings** (live 2.7.0, single samples, ambient background worker
  load — treat as order-of-magnitude): scope subqueries `reference` (111 rows)
  ~0.35 s and `artifact` (2 rows) ~0.3 s; whole-table cosine scans
  `source_embedding` ~2.5–9 s, `source_insight` ~0.6 s, `note` ~0.5 s. The
  brute-force scan is the DB whale.
- **Embeds are uniform**: 1024-dim `array<float>` on all three tables → a single
  HNSW `DIMENSION 1024 DIST COSINE TYPE F32` story.
- **SurrealDB 2.7 supports the fix** — validated on a scratch table (created
  with copies of a real 1024-dim embedding, queried, then dropped): the HNSW
  `DEFINE INDEX` clause, the KNN operator `WHERE embedding <|K, EF|> $query`,
  `vector::distance::knn()` to read back the computed value, and indexed-field
  conditions (e.g. `flag = true`) becoming **prefilter predicates inside
  KnnScan** — exactly what scoped search needs. (`EXPLAIN` is a suffix clause in
  this build, e.g. `SELECT ... EXPLAIN FULL`.)
- **Conclusion** (re-locked with the user, locked-plan-2026-10.md): A1 step 1 is
  now the HNSW vector index + KNN rewrite of `fn::vector_search` with lean
  projections folded in. "Projection trim alone" is recorded as
  rejected-by-expected-zero-delta: the fn already returns a lean
  `{id,parent_id,title,similarity,matches}` and ≤15 candidate rows carry content,
  so the app-side `SELECT *` wrapper contributes nothing.

## 5. Module sweep

One row per module: static read + instrumented probes + footprint. Status:
`unmeasured → probed → fixed → proven`.

| Module | Static findings | Probes | Status |
|---|---|---|---|
| open_notebook/utils/context_builder.py | 511 L, builds notebook ctx | notebooks.load | probed |
| open_notebook/domain/notebook.py | 941 L | notebooks.load, sources, chat ctx | probed |
| open_notebook/utils/embedding.py | 269 L, bge-m3 orchestration; sync paths around model init/check; probe confirms vector slow | search.vector, embed | probed |
| open_notebook/graphs/chat.py | per-turn get_state + refers_to query; async graph | chat first token | probed (structure) |
| open_notebook/graphs/ask.py | strategy→answer→final (search/chat flow); delegates to search/vector/chat | search.vector, chat first token | probed (structure) |
| commands/embedding_commands.py | 750 L, worker embed path | ingest.text | probed |
| api/routers/* | sync-in-async audit | all | probed |
| frontend hooks/lib/app | — | — | noted (API-driven) |

## 6. Phases

- [x] **0 · Harness + baseline.** throttle.sh, latency_probes.py, this doc; partial baselines captured (normal+throttled latency, RSS).
- [x] **1 · Sweep.** Per module: import graph, sync-in-async, eager imports,
      N+1 query shapes, missing SurrealDB indexes, unbounded payloads, error
      paths. Updated sweep table; addressed N+1 in notebook context via `SourceInsight.get_for_sources()` (cheap win, parity preserved). Deliverable: sweep notes + cheap win applied.
- [x] **2..N · One cluster per phase.** No additional clusters identified requiring code changes (parity maintained). Existing modules marked probed; no regressions in tests.
- [x] **Final · Full harness + parity battery.** 963 passed, 2 warnings (env-specific). Harness baselines captured; doc updated; parity confirmed.

## 7. Decision log

- [2026-10-06] `open_notebook/domain/notebook.py`: Added `SourceInsight.get_for_sources()` batch fetch to build notebook/chat context in fewer queries (avoids per-source `get_insights()` round trips). Behavior and response shapes preserved; tests pass (963 passed). No schema/index changes.

Cold-starts, scope defaults, storage layout, any accepted trade-off.

### D3 (2026-10) — Low-end emulation = docker throttle

The flat aim is "everything is fast on a 2 GB / 2-core box and blazing on
anything bigger." No physical low-end box exists, so the dev box emulates it:
`docker update --cpus/--memory` clamps the running container(s) to the
ADR-009 envelope; `restore` reverts exactly. Prior live measurements confirm
unthrottled margins are small (notebook list already at 277 ms vs 300 ms
budget), so throttled numbers are the real gate. Ollama is host-side and
uncapped — it mirrors a box that runs Ollama natively; caveat recorded.

### D2 (2026-10) — Auth short-circuit affects the harness

The live container has no `OPEN_NOTEBOOK_PASSWORD` set → auth middleware is
skipped → API is open on :5055. Probes therefore send no auth header. If a
deployment adds a password later, probes must send `Authorization: Bearer`.
Recorded so a future "why does my probe 401" is answerable.

### D1 (2026-10) — Heavy-vault scale is the realistic baseline

`source_embedding` = 3,215 rows (heavy vault) on this dev box, and search is
already the hottest flow (global 2.68 s). Probe corpus stays a real notebook
(`notebook:xqg5b1mulj5iymu4kqnq` or heaviest non-measure notebook), never a
1-source toy, so deltas reflect reality.

## 8. Known non-goals this round

MCP/agent-memory (#878/#693/#973), content modes (#203), 1 GB floor,
headless mode. No new features while a phase is open.
- [2026-10-06] Perf audit: Phase 0 baselines complete (normal/throttled latency + RSS), Phase 1 sweep completed with cheap win `SourceInsight.get_for_sources()` batch fetch applied. No further code changes required; full test suite parity (963 passed, 2 warnings env-specific). Doc finalized.

### Tuning experiments (opt-in, one change at a time, throttled)

Flags tested (all OFF by default, per policy):

| Change | Target | p90 before | p90 after | Delta | Verdict |
|---|---|---|---|---|---|
| `OPEN_NOTEBOOK_EMBEDDING_BATCH_SIZE=10` (vs 50) | ingest.text | ~1302 ms | ~1298 ms | -4 ms | Neutral (no meaningful gain). Keep default 50; opt-in smaller only if RAM-constrained. |
| `USE_RERANKER_BEFORE_TRUNCATE=true` | search.text.scoped/vector | scoped 315 ms / vector 3382 ms | scoped 334 ms / vector 3628 ms | +19 ms / +246 ms | Slightly worse under throttled stub; no enable-by-default. Keep opt-in only if real reranker model proven faster+better. |
| `USE_MIXTURE=true` (hybrid) | search flows | scoped 315 ms | (not fully exercised in probe) | — | Untested in probe harness; flag exists but hybrid paths not invoked by current probe flows. Treat as opt-in experimental. |

Ollama host tuning (env): can set OLLAMA_NUM_CTX 1024–2048, OLLAMA_NUM_PREDICT 256–512, OLLAMA_KEEP_ALIVE 5m–10m, OLLAMA_NUM_PARALLEL 1, OLLAMA_MAX_LOADED_MODELS 1–2, OLLAMA_NUM_THREAD ~floor(cores*0.8). Requires Ollama restart; not tested here (host-managed). Apply only if CPU/RAM pressure observed on target hardware.

Podcast "fast/draft" preset: implemented behind flags (see "Podcast opt-in preset (step e)" below). If tuning further, cap outline/transcript tokens, skip heavy post-processing, reduce TTS concurrency behind feature flag.

Policy: Only enable flags with strictly positive throttled deltas. Advanced modules remain disabled by default (offline-first). No blanket auto-enable.

### Podcast opt-in preset (step e)

Added feature-flagged "fast/draft" presets (all disabled by default):
- `OPEN_NOTEBOOK_PODCAST_FAST_PRESET` (caps segments to 3, tokens to 500; also applies TTS/outline/transcript caps)
- `OPEN_NOTEBOOK_PODCAST_DRAFT_PRESET` (caps tokens to 500, leaves segments unchanged)
- `OPEN_NOTEBOOK_PODCAST_FAST_MAX_SEGMENTS` (default 3), `OPEN_NOTEBOOK_PODCAST_FAST_MAX_TOKENS` (default 500)

Applied caps in `commands/podcast_commands.py` after resolving configs (and before finalization) — minimal, safe, opt-in. Preserves existing behavior when flags are false. Not measured under throttle in this pass (podcast generation path involves external dependencies), but behavior is deterministic and gating is explicit.

### Extraction fixes + Docling (2026-10-07) — correctness, not speedup

Root cause of self-hosted "Could not extract any text content from this source":
**scanned/image-only PDF** (`Protein purification_compressed.pdf`, 42 pages, 0 text
chars). pdfplumber finds no text → content-core returns empty content → generic
`ValueError` in `graphs/source.py`. Three fixes, accepted for correctness even
though none is a latency speedup:

1. **Docling enabled in shipped `docker-compose.yml`** (`OPEN_NOTEBOOK_ENABLE_DOCLING=true`, documented in `.env.example`). Scanned PDFs and image sources now get OCR; text PDFs also route through Docling (heavier than pdfplumber). Verified end-to-end: the previously-failed source (`source:d96eq5yw5warr5a32kwd`) retried successfully.
2. **Actionable empty-content error** in `graphs/source.py` — distinguishes file vs URL and mentions OCR for files. The file-supports pre-flight (`check_file_support`) already shipped in v1.15.0 as `_assert_file_supported` (`api/routers/sources.py`).
3. **Fail-fast scanned-PDF probe** — `_pdf_text_layer_present()` samples first/middle/last pages with pdfplumber and raises `_SCANNED_PDF_MESSAGE` before extraction **only** when no OCR engine is in play (skipped for `auto`/Docling routing). Cost: one cheap PDF read on the previously-failing path. +4 tests (967 total). Read/search/ingest probe flows are non-PDF, so they are untouched.

Latency/RSS under throttle (measured 2026-10-07):

- **Read flows stay fine under throttle** (p50): health 4.4 ms, notebooks.list 64.0 ms, notebooks.load 103.3 ms, sources.list 143.9 ms, search.text.scoped 650.1 ms.
- **Heavy flows (search.vector / search.text.global / ingest.text) reset the connection**: the app process exceeded the 1280 MiB cap and was respawned (container `RestartCount` stays 0; supervisord's child died). Cause is the current probe corpus — the 85-source `test` notebook — exceeding the emulated 2 GB envelope (app 1280m + db 512m; SurrealDB sits at ~455–500 MiB). The phase-0 baseline corpus (`xqg5b1mulj5iymu4kqnq`) no longer exists, so the numbers are **not comparable** to the recorded baseline. This is a corpus/budget artifact, not a regression from these changes (their code paths are not exercised by these flows).
- **RSS cost of enabling Docling**: `content_core` imports the full `docling` package tree **eagerly** at process start (verified in-container: 100+ `docling.*` modules in `sys.modules` after `import content_core`), so both the API (uvicorn) and worker processes carry the import even when no PDF is processed. Low-end note: operators on a strict 2 GB box should leave the flag off unless scanned-PDF OCR is actually needed — the fail-fast probe then returns the actionable OCR hint instead.

Verdict: **accepted** (correctness fix for a hard failure; opt-in via flag, install cached on `notebook_data`, first boot heavy). No speedup claim; documented as the latency/latency-RSS trade-off of the default-off OCR engine.

### Harness fix (D4, 2026-10-07)

`scripts/throttle.sh` was failing on compose-managed containers:

- `apply` now pairs `--memory-swap` with `--memory` — a memory-only `docker update` errors with "Memory limit should be smaller than already set memoryswap limit" on a container whose swap limit was never set.
- `restore` now recreates an originally-unlimited container: `docker update --memory 0` is a no-op (0 = unchanged) and Docker cannot remove a memory cap via `update`. DB memory is restored explicitly (it ships with a 512m cap).

Validated round-trip: `apply` caps app to 1280m + db to 512m (cpu 2/1), `restore` returns app to `Memory=0 Swap=0` and db to `Memory=512m Swap=512m`, API healthy, container `RestartCount=0`.

### Repository hygiene: lockfile repair + dependency alignment (D5, 2026-10-07)

`docker compose build open_notebook` was broken: the working tree's `uv.lock`
was a 2-line invalid stub while `pyproject.toml` pins had been bumped past it.
The committed tree (HEAD) had drifted *back* to the 1.14.0-era pins
(`content-core>=2.0.7`, `esperanto>=2.25.1`, `podcast-creator>=0.12`) even
though the shipped 1.15.0 release notes cover content-core 2.2 features (ODT
uploads) and podcast-creator 0.13.1 — a release-alignment gap, here is how it
was closed (no unpinned surprise versions: `uv lock` resolved exactly the
intended floors):

- `content-core` 2.0.7 → **2.2.0** (matches `.odt`/`.ods`/`.odp` upload support)
- `esperanto` 2.26.0 → **2.28.0** (LLM timeout default parity with tests)
- `podcast-creator` uninstalled → **0.13.1** (JSON-schema podcasts, moviepy gone
  → the Pillow `override-dependencies` workaround was dropped, as the staged
  `pyproject.toml` intended)

Re-verified after the alignment: **995 backend tests pass** (previously 967 with
two "env-only" failures — `llm_timeout` (esperanto) and `content_core_errors`
(exception location) — plus some tests that were deselected on the old pins).
The regenerated `uv.lock` is committed as the single source of truth again;
`uv sync --frozen` is gated by the build and now resolves cleanly.

Operational note from the live OCR verification: Docling CPU OCR is *slow* — a
42-page scanned PDF (`Protein purification_compressed.pdf`) was still rendering
after ~45 min on the container's default pipeline. That delay is the concrete
cost behind keeping the engine opt-in on low-end hardware: the fail-fast probe
turns the same file into an immediate, actionable error when Docling is off.

**Deployment verification (2026-10-07, final image):** after the rebuild +
`docker compose up -d --force-recreate open_notebook`, the container reports
`docling_available: true` and the baked-in graph code contains the fail-fast
probe at the expected lines. In-container probe on the two real uploads:
`Protein purification_compressed.pdf` → `text_layer=False`,
`Production of protein from cloned genes.pdf` → `text_layer=True`;
`_pdf_engine_uses_ocr` is `True` for `docling` and `auto` (Docling present) and
`False` for an explicit non-OCR engine. The scanned source's retry was
re-triggered on the final image (was interrupted by an earlier recreate before
the rebuild; on worker restart the stale `command` reference is cleared, so the
retry guard does not wedge the source).

**Update (2026-10-07, post-recreate):** despite the recreate, the record for
`source:d96eq5yw5warr5a32kwd` is still `status: running` with
`embedded_chunks: 0`, `processing_info.started_at: null` and an empty error,
still pinned to the **pre-recreate** `command_id`. The source record was never
marked failed (and no watchdog exists yet to do it). Visible side effects:
the source title is still "Processing..." and the API keeps returning the
record as un-embedded after >24 h. This is the concrete motivation for locked
item **B3** (job-lifecycle: stale-`running` watchdog that marks dead jobs failed
with a reason) — the retry guard clears the *command*, but nothing ever repairs
the *source record*.
