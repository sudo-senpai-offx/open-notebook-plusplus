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

**Status:** baselines captured (unthrottled + throttled latency probes, container RSS; A2 measurement records below).

### Latency (normal) — recorded from `scripts/latency_probes.py --mode normal`

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
| search.vector | 3363.7 | 3381.5 | 3373.1 | 500 | **OVER BUDGET (6.8x p90)** — vector is heavy; tune via opt-in flags only |
| search.text.global | 347.5 | 350.0 | 350.5 | 500 | OK |
| ingest.text | 1282.7 | 1295.9 | 1294.7 | 1000 | OK |

### RSS + startup (ADR-009)

Earlier readings (partial): API 562.6 MiB measured unthrottled; worker / node
baseline in progress. A2 #1 closed the worker + node cells and extended capture
to whisper/piper (2026-10-07, live throttled stack).

**A2 #1 re-measurement (2026-10-07, envelope: app 2 GiB / 2c, db 512 MiB / 1c):**

| Component | Idle RSS | Task peak | Envelope | Status |
|---|---|---|---|---|
| API (`uvicorn`, VmRSS) | **~878 MiB** | ~878 MiB (flat) | <220 MB / <5 s | **measured — over budget** |
| Worker (`surreal-commands-worker`, VmRSS) | **~865 MiB** | **~865 MiB** (embed 12 chunks: +8 KB) | <300–500 MB | **measured — over budget** |
| Frontend (`next-server`, VmRSS) | **~52 MiB** | — (SSR) | <150 MB | measured — OK |
| SurrealDB (container) | **~443 MiB** / 512 MiB | — | — | measured |
| Whisper (container) | **~50 MiB** | ~50.9 MiB (idle through podcast run — TTS routes to piper-en) | — | measured (idle) |
| Piper (container) | **~4 MiB** | **~1054.7 MiB (1.03 GiB)** — 68 s TTS window, A2 #2 | — | **measured — TTS peak** |
| Piper-JA (container) | **~16 MiB** | ~16.4 MiB (idle — English voice) | — | measured (idle) |

Container working set (the binding number for the 2 GiB envelope): docker stats
shows the app container at **1.62 GiB / 2 GiB** (cgroup `memory.current` =
2046 MiB incl. reclaimable cache; RSS across api+worker+node ≈ 1.86 GB). Two
samples 10 s apart are identical — the footprint is a stable loaded working
set, not a leak in motion. But the stack now **idles at / just under the
envelope ceiling**, vs ~430 MB in the original ADR-009 session.

Caveats: per-process VmRSS includes shared text pages and freed-but-not-returned
pymalloc arenas, so it grows with uptime and double-counts shared wheels; the
container-level number is what the 2 GiB cgroup actually enforces. The growth
vs ADR-009's original ~170/~150 MB is a real modded-fork regression (eager
`docling` + content-core/esperanto/podcast-creator imports land in both api and
worker RSS — see the Docling trade-off note below), and it is the likely reason
earlier transient `KeyError`/`Future cancelled` flakiness appeared under
memory pressure. A2 #2/#5 (podcast, chat-with-ollama on the same box) start
with only ~380 MB app-container headroom — measure and record, they will
likely need the ADR-009 stage-4 footprint cuts to fit.

#### A2 #2 podcast generation baseline (2026-10-07 — baseline-only, no gate)

Job `command:nu8zbqbbwh7lucqr5g3d`, episode `a2-podcast-baseline`
(`episode:pu87uek777eutolcjp0n`), profile `solo_expert` (outline produced 5
segments), **default config — the fast/draft preset knobs remain OFF** (they
are the documented opt-in tuning path, not exercised here). Envelope verified
still applied at end of run: app 2 GiB / 2c, db 512 MiB / 1c. LLM leg: ollama
`mistral:latest` (7.2B) host-side (12c, unthrottled — caveat below); TTS:
`en_US-amy-medium` via piper `:9600`.

**Timeline** (log timestamps UTC; submit 11:39:30.688 → success
12:14:15.205):

| Phase | Window | Duration | Share | Retries |
|---|---|---|---|---|
| queue → worker pickup → outline start | 11:39:30.7 → 11:39:33.9 | 3.2 s | — | — |
| outline (5 segments) | 11:39:33.9 → 11:47:14.1 | 460.2 s | 22.1% | 1 × 180 s ReadTimeout (11:42:34) |
| transcript (5 segments) | 11:47:14.1 → 12:13:04.7 | 1550.6 s | 74.4% | 1 × 180 s ReadTimeout (11:50:14, seg 1) |
| TTS (38 clips, 8 batches × 5) | 12:13:04.7 → 12:14:12.9 | 68.2 s | 3.3% | — |
| combine + finalize | 12:14:12.9 → 12:14:15.2 | 2.3 s | ~0% | — |
| **total** | | **2084.4 s (34.7 min)** | 100% | **2** |

Per-segment transcript times: 398.6 / 228.7 / 253.0 / 380.3 / 290.0 s (seg 1
includes its retry). Output: 6.12 MB MP3, 38 clips. Poller-observed status
wall time 2077.7 s (10 s poll granularity).

**Findings:**

- **LLM = 96.5% of wall time** (outline + transcript 2010.8 s of 2084.4 s);
  TTS is 3.3%; app-side orchestration (queue pickup, batching, combine) ≈ 6 s.
- **180 s provider read timeout is systemic for 7.2B generation on this
  host**: 2 retries (outline attempt 1, transcript seg 1 attempt 1), both
  recovered on retry. Retry budget = max 3 attempts, 5 s × 2ⁿ backoff, 30 s
  cap — never exhausted. This is a baseline property, not a stack fault:
  whole-segment generations regularly exceed the read timeout, so any timing
  comparison must expect ~1 retry in the early long-generation phases.
- **Envelope held end-to-end**: app container working set peaked at
  **1455.1 MiB / 2 GiB (71%)**, db at **426 MiB / 512 MiB**; app
  `RestartCount=0`, worker pid 222 and uvicorn pid 30 unchanged across the
  run, no OOM, job completed. (db `RestartCount=1` predates the job — the
  11:00 UTC stall incident already recorded under A2 #1.)
- **Piper TTS is the memory outlier: 4.2 MiB idle → 1054.7 MiB (1.03 GiB)
  peak** during the 68 s TTS window (raw samples at ~15 s: 671.9 / 725.4 /
  884.5 / 1006 / 1054.7 MiB, decaying after), vs its 3.562 GiB compose limit.
  Whisper (peak 50.9 MiB) and piper-ja (peak 16.4 MiB) stayed at idle — the
  English voice routes to piper-en only. **Fit implication:** the concurrent
  stack during TTS ≈ app 1.345 GiB + db ~0.35 GiB + piper 1.03 GiB ≈ 2.7 GiB
  across per-container limits on this 16 GB host — fine here, but it would
  **not fit a single 2 GB physical box**. TTS phase is the low-end fit risk;
  queue ordering (transcript completes before TTS starts) keeps app below its
  own peak while piper runs.
- App container dips to 1.345 GiB during TTS (from its 1.421 GiB mid-run
  peak): clean file pages reclaimed under the 2 GiB cgroup while piper demands
  memory — the same eviction dynamic as the VmRSS caveat below.

**RSS caveat (A2 #1, now observed under load):** mid-run worker/api VmRSS
peaked at **637.7 / 655.4 MiB — *below* their idle readings (865 / 878
MiB)**: the 2 GiB cgroup reclaims clean shared/cache pages under pressure, so
VmRSS is not the binding number — container working set (1.40–1.46 GiB) is.
Capture coverage: original samplers 19:39:47–19:51:07 (outline + seg 1–2),
sampler 3/4 pairs 19:54:51–20:18:38 (seg 2 → terminal). Worker VmRSS has a
~5 min gap 19:49:50–19:55:06 (flat 628.9 → 637.7 MiB on both sides); worker2's
`worker=` column self-matched pgrep and is discarded, but its `api=` column is
valid (peak 655.4 MiB, consistent) and closes the api gap.

**LLM-leg caveat:** ollama runs host-side (12c) and is *not* CPU-throttled by
the envelope (the cgroup caps app + db only) — this baseline measures app-side
fit and queue behavior under the envelope, not low-end generation speed. Model
knob stays default (`mistral:latest`); the podcast fast/draft presets and a
smaller model are the opt-in paths if 34.7 min must come down.

#### A2 #3 container cold starts (2026-10-07 — measure & record)

Method: `docker restart open-notebook-open_notebook-1` ×3 back-to-back under
the envelope (verified 2 GiB / 2c before and after; db 512 MiB / 1c untouched),
no samplers running (prerequisite). Clocks: container `StartedAt` (ns) as t0,
`docker logs -t` prefixes for in-boot markers, external polls of `GET /health`
and `GET :8502` at 0.25 s. One additional sample was taken on the *first*
restart after 3 h 20 m of uptime (harness v1 — its API/worker log-marker
extraction is valid; its frontend poll had a false-success bug fixed before
these three runs). DB migration check reads **v29 on every boot** (image max
= 28 → no-op).

| Marker (from container start) | run 1 | run 2 | run 3 |
|---|---|---|---|
| entrypoint start | +0.46 s | +0.43 s | +0.42 s |
| supervisord spawn (api / worker / frontend) | +1.69 / 1.69 / 1.70 s | +1.63 / 1.63 / 1.64 s | +1.63 / 1.63 / 1.63 s |
| worker banner ("Starting Surreal Commands worker") | +3.40 s | +3.23 s | +3.14 s |
| migration check (v29, no-op) | +14.94 s | +14.72 s | +14.53 s |
| API ready (`Application startup complete`) | +14.98 s | +14.78 s | +14.58 s |
| worker ready (`Starting LIVE query listener`) | +14.82 s | +14.46 s | +14.36 s |
| API `/health` 200 (external poll) | +15.18 s | +15.17 s | +14.92 s |
| wait-for-api done → frontend start | +16.78 s | +16.71 s | +16.80 s |
| Next.js ready (`✓ Ready in 0ms`) | +17.17 s | +17.13 s | +17.21 s |
| frontend HTTP (external poll) | +17.55 s | +17.50 s | +17.60 s |

Derived: worker **spawn→ready 13.13 / 12.83 / 12.73 s** (budget `< 15 s` →
**PASS ×3**); API spawn→ready 13.29 / 13.15 / 12.95 s; `docker restart` command
window (stop grace + start) 2.63 / 2.15 / 2.25 s; issue→`/health` 17.58 /
17.09 / 16.94 s; issue→frontend serving 19.95 / 19.42 / 19.62 s.

**Findings:**

- **Worker `< 15 s` (ADR-009 stage 2): met on every measured restart** —
  12.7–13.1 s spawn→ready steady-state (+14.4–14.8 s from container start,
  which includes supervisord + `uv run` overhead). Back-to-back restarts are
  tight (±0.3 s). The first-restart-after-idle sample measured **18.9 s**
  spawn→ready — over target; cause not isolated (first restart after 3 h 20 m
  uptime and the podcast run's memory pressure — page-cache/host-load
  variance, not reproducible in the warm runs). Recorded as variance, not a
  steady-state failure.
- **API startup is import-bound, not migration-bound**: spawn→ready ≈ 13 s,
  of which the migration check itself is a ~0.05 s v29 no-op. The wall is the
  shared heavy module graph — API and worker import all command modules
  *concurrently* on 2 CPUs. If the ADR-009 `< 5 s` API budget is read
  container-side it is missed by ~8 s; that budget belongs to
  `measure_resources.sh` spawn mode (its own process scope), which A2 #3
  deliberately replaced with container-level timing per the locked decision —
  the two are not directly comparable, and the harness was not re-run here.
- **Frontend has no build at boot**: the image ships the prebuilt standalone
  output; `wait-for-api.sh` (5 s poll cadence) hands off 1.8–2.2 s after API
  ready, then `node server.js` is ready in **0.39–0.41 s**. Total
  issue→serving ≈ **19.4–20.0 s**, dominated by API readiness.
- **Cold-start DB safety**: v29 no-op on all boots — the image (max 28) can
  no longer re-apply migration 28, so the 09:28 boot's drift re-application
  cannot recur; no migration work runs at cold start.
- Envelope confirmed unchanged after the runs (`NanoCpus=2e9`,
  `Memory=2147483648`); `RestartCount` stays 0 (manual `docker restart` does
  not increment it).

#### A2 #5 chat turn under envelope (2026-10-07 — measure & record)

Flow: opt-in `chat.turn` in `scripts/latency_probes.py` (excluded from the
default `all` discovery — the local-model flow only runs when asked for).
`POST /api/chat/sessions` → `POST /api/chat/execute` → session `DELETE`; one
terse turn ("Reply with the single word: OK") with an explicitly empty context
(`sources: [], notes: []`), so the figure covers session resolution + notebook
link + local model call + full response — notebook content never enters the
turn (session links to the auto-discovered probe notebook, the 85-source
`test` notebook; `fixture-*` notebooks are excluded from discovery).

**Ollama wiring (locked A2 #5 decision):** for the duration of the run ollama
was CPU-quota'd to **2 CPUs** (`systemctl set-property ollama.service
CPUQuota=200%` → cgroup `cpu.max=200000 100000`), matching the app's 2c share,
so the inference leg competes on the same CPU budget as everything else.
`MemoryMax` was left at `infinity`: the default chat model is
**aya-expanse:8b (5.1 GB)** and no low-end-plausible cap can hold it —
`throttle.sh`'s declared `THROTTLE_OLLAMA_MEM=768m` default is aspirational
(ollama is a host systemd service, never a container; the script's `apply`
only touches the app + db containers) and applying it would OOM the model
load rather than throttle it. Clamp reverted immediately after the run
(`cpu.max` back to `max`, drop-in removed); standing policy **D3 (ollama
host-side, uncapped)** is unchanged — this was a one-run wiring per the
locked decision.

Default chat model confirmed via `GET /api/models/defaults` →
`model:nim5nvu897dikcxy4dji` = **aya-expanse:8b** (ollama, host). No model
resident at run start (`ollama ps` empty) — the discarded warmup beat absorbed
the cold load. Per-request probe timeout 120 s (`ESPERANTO_LLM_TIMEOUT=300`).

| Flow | n | mean | p50 | p90 | min | max | errors |
|---|---|---|---|---|---|---|---|
| chat.turn | 4 (+1 warmup) | 8482.2 | 9430.6 | 9951.1 | 4461.1 | 10085.9 | **0** |

(millisecond figures; `--mode throttled`, `-n 5`, first beat discarded as
warmup; raw: `/tmp/opencode/a2-chat-throttled.txt`)

**Findings:**

- **0 timeouts, 0 HTTP errors.** The guardrail ("timeout counts recorded
  separately from app latency") had nothing to separate this run: the probe
  records transport/HTTP failures in `errors`, never as beats, so a
  starved-model timeout could not have been folded into the latency columns
  even if one had occurred.
- **Budget row ("chat first token < 2 s") is not measurable on this surface.**
  `POST /api/chat/execute` does not stream, so first-token latency cannot be
  isolated; per the locked decision the **full-turn** figure is recorded:
  p50 **9.43 s**, min **4.46 s**, max **10.09 s** (2c ollama + aya-expanse:8b).
- Beat spread 4.46 s vs 9.4–10.1 s — cause not isolated (model load / page-in
  ordering, prefill variance); n=4 too small to attribute.
- Verdict: **recorded, not gated** (ADR-009 explicitly scopes A2 #2/#5 this
  way) — the budget is *unverifiable* on the non-streaming REST surface, not
  measured-and-missed. A first-token number needs a streaming surface (SSE /
  graph stream mode); candidate follow-up, outside the locked A2 scope.

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

#### A1 experiment-1: throttled before-baseline + plan/schema evidence (2026-10-07)

Throttled envelope applied (`scripts/throttle.sh apply`: app 2 cpu / 1280m,
surrealdb 1 cpu / 512m; state `/tmp/opencode-throttle-state.txt`). Baseline
captured with `scripts/latency_probes.py --flows
search.vector,search.vector.decomposed -n 5 --mode before-hnsw --json` (warmup
beat discarded) plus golden result captures for the same query ("the
importance", default `limit=100`, `minimum_score=0.2`) at
`/tmp/opencode/a1-hnsw-golden/before_{test85,fixture}.json`:

| Corpus | Flow | p50 | mean | p90 | hits |
|---|---|---|---|---|---|
| test85 (85 srcs) | search.vector | 3531.9 | 3572.7 | 3599.6 | 22 |
| test85 (85 srcs) | .db phase | **3194.0** | 3215.8 | 3206.0 | |
| fixture (4 srcs) | search.vector | 2458.0 | 2511.1 | 2490.3 | 4 |
| fixture (4 srcs) | .db phase | **2069.0** | 2069.0 | 2074.0 | |

The before-hnsw goldens carry ranked id / parent_id / similarity / title /
matches samples to diff top-K recall against the after-probe.

**Plan-evidence battery** (scaled scratch table, 300 × 1024-dim rows copied from
`source_embedding`, HNSW `DIMENSION 1024 DIST COSINE TYPE F32 EFC 40 M 12`,
`EXPLAIN FULL` suffix clause):

- **KNN alone stays graph-driven.** `WHERE embedding <|200,200|> $query` →
  `Iterate Index: idx_..._hnsw <|200,200|>`. Dropping the migration-27
  `embedding != NONE` guard does **not** regress the plan, so it is removed too
  (the index omits NONE rows, and 2.7 hard-errors wrong-dim inserts, so every
  indexed row is a valid 1024-dim vector).
- **The `array::len(embedding)=array::len($query)` guard is dropped**: EXPLAIN
  proves it forces `Iterate Table` + `Fallback`, dead-walking the exact scan.
- **Scope/threshold filters must NOT live in the KNN WHERE.** On 2.7,
  `WHERE embedding <|K,EF|> $q AND src IN $scope` plans as `Iterate Index:
  idx_..._src` and returns `[]`; the swapped order stays on HNSW but returns
  wrong rows (1 hit vs the exact 5). The validated shape is a **subquery wrap**:
  the inner KNN select projects `vector::similarity::cosine(embedding, $query)
  AS similarity` (+ id / title / content / parent_id / scope key); the outer
  applies `WHERE (!$scoped OR scope_key IN $scoped_sources) AND similarity >=
  $min_similarity ORDER BY similarity DESC LIMIT $match_count`.
- **Fetch-K = 200 / EF = 200 fixed literals** (KNN requires unsigned-int
  literals — variables are a parse error — and EF must be ≥ K). Covers default
  `limit=100` plus headroom; `match_count > 200` truncates to the fetch window
  — a documented approximation, re-checked in the after-probe. On the 300-row
  sample, inner `K=200` + outer scope/threshold returned the **byte-identical
  top-5 (ids + similarity values) to the exhaustive scoped baseline**; 16 of the
  24 in-scope rows fell inside the fetch window.
- **Schema contract change (tests).** HNSW `DIMENSION 1024` enforces 1024-dim
  embeddings at write time (`Incorrect vector dimension` on mismatch — verified
  both on live 2.7 and on the embedded `mem://` engine the suite uses). The
  migration-content fixture in `tests/test_text_search_parent.py` used 2-dim
  `[1.0, 0.0]` vectors; those become 1024-dim arrays (query vector == embedded
  vector, assertions unchanged). `test_migration_25_up_down_up` applies only
  migration 24, so its 2-dim fixture is untouched.

#### A1 experiment-1 keep/rollback gate: envelope capacity + parity verdict (2026-10-07)

**OOM was envelope capacity, not migration 28.** After `--force-recreate` (which
re-drops docker limits to compose defaults) the app ran uncapped and idle
footprint was ~1,286 MiB — i.e. it no longer fit the 1,280m throttle cap, so a
vector search (which briefly stretches RSS) tripped `exit 137`. Proof: text
search survived the same cap; api never runs HNSW (surrealdb does); process list
was clean of orphans; light GET + text search OK. After re-applying the throttle
the app immediately OOM'd again — the *envelope* no longer fit, so the F flag is
on the envelope, not the migration.

**Envelope correction (applied live, kept for every A1 probe below):** app
memory raised to **2 GiB** (CPU stays 2c; surrealdb stays 1c/512m). The stack
then runs vector search end-to-end. Rationale: 2 GiB is the smallest raise that
fits idle+peak; db verbatim at 512m proves DB memory is not the constraint.
(Note: `docker update` limits do not survive `compose up --force-recreate`;
deploys still rebuild the image.)

**Verdict: REJECT A1 step 1 — roll back to migration 27 at the gate.** The
locked keep-condition is *strictly positive latency delta* **AND** *parity
(≥ same hits, output shape intact)*. The db-phase delta is strictly positive
(**818 ms vs 3194 ms baseline p50, −74%**) and shape is intact, but parity
**fails on both corpora**:

| Corpus | baseline (fn27) | after (fn28) | delta |
|---|---|---|---|
| test85 (85 srcs) | 22 hits | **17 hits** | −5 |
| fixture (4 srcs) | 4 hits | **0 hits** | −4 |

Determinism confirmed (repeat A/B runs return identical hit sets; the deltas are
envelope-independent — hits are a pure function of fn + data + query params,
identical bodies: `limit=100`, `minimum_score=0.2`).

**Root cause (authoritative SQL reproduction on live 2.7, same bge-m3 query
vector):** fn28's inner KNN runs a **global, per-table fetch window**
(`<|200,200|>` → actually returned 190 rows, worst-sim ≈ 0.4289, because HNSW
returns ≤ K candidates) *before* the outer scope/threshold filter. Exact-scan
hits whose sim sits below the window floor fall out:

- fixture's 4 hits (sim 0.4036–0.4189) are **all below the window floor
  0.4289** → fn28 returns 0. A small notebook whose matches are merely "good,
  not stellar" loses everything; this is a recall cliff, not drift.
- test85 loses 6 parent sources (sim 0.4342–0.4591) plus a chunk-selection
  artifact (same source surviving via a lower-scoring chunk, e.g. 0.4911 →
  0.4896). 9 source-level diffs observed at the SQL level vs the 5 lost at the
  API level.

**Rejected rationale (recorded per locked plan):** migration 28's own comment
flags rows beyond the fetch window as "a documented approximation" — that
approximation violated the parity half of the gate on both corpora. The window
is a hard cap that scales with table size / query crowdedness, independent of
envelope. EF/K bump (e.g. K=1000/EF=1000) is **not in scope** for A1 step 1 and
would still be an approximation with a partial-footprint tradeoff; per the plan,
parity failure = roll back + record, then re-sequence. **Action taken:
`run_one_down()` applied 28_down (HNSW indexes removed, exact-scan fn27
restored verbatim); live fn27 re-verified to return 22 / 4 hits.**

**Durable seal of the rejection (migration 29, landed live 2026-10-07):** the
live cluster had drifted back to the rejected state — because migration 28 is
baked into the container image, the rejected HNSW + KNN fn was **re-applied on
container restart** (DB at version 28, `INFO FOR DB` showed the KNN operator
again, no 27 len-guard), re-creating the parity loss until stopped. Migration
29 makes the rejection terminal: `run_all()` re-applied 28_down's statements
(fn27 exact-scan restored verbatim, 3 HNSW indexes removed) and bumped the DB
to **version 29** with `needs_migration = false`, so no redeploy ever re-runs
28. Verified on the live DB: version 29, exact-scan `fn::vector_search`
(len-guard + `>= min_similarity` present, no `<|...|>` KNN operator), HNSW
indexes absent. Post-seal parity probe on `test` (85 src, "the importance",
`limit=100`, `minimum_score=0.2`): **22 hits**, shape
`{id, parent_id, title, similarity, matches}` — matches the recorded baseline.
29 down (29_down.surrealql) restores 28's HNSW/KNN state byte-for-byte, so the
28/29 pair stays fully reversible for audit. Deploy note: the baked image still
carries 28/29; a fresh deploy applies 29 at first boot once the DB is at 28 —
the DB is now ahead, so restarts are safe.

**Seal re-verified after a SurrealDB container restart (2026-10-07):** the DB
container restarted cleanly mid-fixture-provisioning (`restart: always`; exit 0,
not OOM — 512 MiB / 1 CPU envelope untouched). The migration table survived
unchanged (`_sbl_migrations` = 1..29), and `INFO FOR TABLE` confirms the seal
holds: `idx_source_embedding_hnsw` / `idx_source_insight_hnsw` / `idx_note_hnsw`
absent on all three embedding tables, migration-1 BM25 `SEARCH ANALYZER`
indexes intact, `fn::vector_search` still the exact scan (len-guard +
`>= min_similarity`, no KNN operator). The migration version lives in the DB
(not the container), so a DB restart cannot re-apply 28 — the durable seal is
restart-proof. (Note: `INFO FOR DB` exposes functions/analyzers but **not**
indexes; indexes are per-table via `INFO FOR TABLE`.)

**Worker stall incident — root cause + fix (2026-10-07):** after that DB
restart, the `surreal-commands-worker` supervisord program kept running but
quietly stopped consuming the command queue: new ingest commands stayed in
status `new` forever and the sync source-create path timed out at 300 s
("Command … did not complete within 300 seconds"), which failed fixture
provisioning twice (source 1, both attempts; partial notebooks cleaned up).
Cause: the worker holds a long-lived SurrealDB subscription; the DB restart
invalidate it and the worker never reconnected. Fix (no code change):
`kill -TERM <worker pid>` → supervisord `autorestart=true` respawned it with a
fresh connection; the two stuck commands immediately ran to `completed` and the
queue drained. Watch item for A2: a DB restart silently wedges the worker; after
any DB bounce, verify the queue drains (`SELECT … FROM command WHERE status =
'new'`) rather than assuming ingest works.

**A2 #6 fixture re-provisioned + 4-hit parity re-check (2026-10-07):**
`scripts/create_fixture_notebook.py --sources 4` succeeded on the healthy
worker → `notebook:1n4cb4qr744izp5xdo26` (fixture-throttle-…, 4 sources / 9
chunks, embedded_chunks 2/2/3/2). Scoped `search.vector` on the fixture
("the importance", default limit/min_score): **4 hits, shape
`{id, matches, parent_id, similarity, title}`** — exact match to the recorded
golden (4 hits / sim 0.4036–0.4189). test85 re-probe in the same run: **22
hits**, same shape (baseline 22). The fixture is now the throttled corpus for
A2 heavy flows per the locked plan.

**A2 #6 heavy-flow status check (2026-10-07 — close-out):** the original
defect (heavy flows reset the connection: the app process exceeded the old
1280 MiB cap on the 85-source corpus) re-tested against the fixture under the
live envelope (app 2 GiB / 2c, db 512 MiB / 1c, verified before + after):
`latency_probes.py --flows
search.vector,search.text.scoped,search.text.global,ingest.text
--notebook-id notebook:1n4cb4qr744izp5xdo26 --mode throttled-fixture -n 3`
→ **all four flows completed, errors 0** (no resets, no timeouts):

| Flow | p50 | mean | max |
|---|---|---|---|
| search.vector (fixture) | 2926.2 | 3005.8 | 3085.3 |
| search.text.scoped (fixture) | 724.3 | 11386.4 | 22048.5 |
| search.text.global (whole KB) | 746.4 | 747.2 | 747.9 |
| ingest.text (probe notebook, self-deleted) | 1567.0 | 3203.9 | 4840.8 |

First beats in each flow absorb cold starts (scoped search max 22.0 s vs
p50 0.72 s; ingest max 4.8 s vs p50 1.6 s) — recorded as-is, warmup not
separately flagged by the harness in this run. No stray `probe-*`/
`measure-*` notebooks left behind. **A2 #6 closed: fixture is the throttled
corpus and heavy flows run on it without resets.**

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

### B3 — stuck-command job-lifecycle pass (2026-10-08)

Lands the fix motivated at the end of §D5: a `running` command (and its source)
that dies with a restart is now repaired **with a reason**, surfaces progress,
and can be cancelled cooperatively. One coherent pass — no new default flags.

**1. Progress.** Stage-level increments written through the existing
`command.progress` path (top-level fields on the schemaless `command` table:
`progress`, `stage`, `heartbeat_at`, `cancel_requested`, `cancel_requested_at`;
terminal writes touch only `{status, result, error_message}`, so the fields
survive). Command: 5 (starting) → 10 (extracting) → 90 (finalizing) → 100
(completed). Graph: `content_process` 10, `save_source` 40 → 55,
`transform_content` 70 → 75. `embed_source`: 5 → 10 → 100. Surfaced unchanged
through `api/routers/sources.py` (`processing_info.progress` / `.stage`).

*Rejected:* **page-level** OCR increments. Docling 2.124 / `content_core`
expose no per-page hook; the only options were patching the third-party
converter (fragile across upgrades) or forking the pipeline. Stage-level
granularity fits the existing structure and is what shipped.

**2. Cooperative cancel.** `DELETE /api/commands/{id}` MERGEs
`cancel_requested`; the job calls `raise_if_cancelled()` at stage/node
boundaries and raises `JobCancelledError`, added to the `stop_on` set of the
`process_source` retry config and the embedding retry config only. The command
library never writes a `canceled` status, so a cancelled job surfaces as
`failed` with "Canceled by user request" (truthful — the run stopped). It cannot
interrupt an in-flight Docling conversion (documented limitation).

**3. Restart watchdog.** `api/job_monitor.py`, one loop started in the API
lifespan (`OPEN_NOTEBOOK_JOB_MONITOR_SECONDS`, default 60). Reads active
(`new`/`running`) rows with a column-limited SELECT (LIMIT 2000) and marks a
`running` row failed when `heartbeat_at` is missing or older than
`OPEN_NOTEBOOK_STALE_JOB_SECONDS` (default 300), requiring **two consecutive
passes** to confirm. Error message `stale: no heartbeat (worker restart, crash,
or hang)` — the reason the old behavior lost. On the pass where a tracked job
leaves the active set, the monitor emits a lifecycle event through
`open_notebook/utils/notify.py` (log-only today; D1 attaches ntfy). The
heartbeat is a **separate thread** (`OPEN_NOTEBOOK_HEARTBEAT_SECONDS`, default
30) precisely because Docling blocks the event loop; its first write lands at
command start and it is stopped in `try/finally` on every exit path.

**Verification (2026-10-08, live throttled stack — app 2 GiB / 2c, db 512 MiB / 1c):**

- **Watchdog proved live.** On the first recreate after the change the monitor
  marked **25 zombie `running` rows failed**: pass 1 baselined them (no
  emission), pass 2 (`06:33:42`, ≈60 s later) merged `status:failed` + the stale
  reason, pass 3 emitted 25 log-only `failed` events. The `d96eq5yw5warr5a32kwd`
  class of zombie is now repaired with a reason instead of silently lost.
- **Heartbeat thread writes land against the live DB** — a 1 s-interval probe
  advanced `heartbeat_at` 4/4 ticks, so a long stage with no progress writes
  (the exact OCR scenario) stays fresh and cannot be falsely marked stale.
- **Parity green** — 1017 tests (1007 baseline + 10 new lifecycle tests),
  `ruff check` / `format`, `mypy` clean.

**Latency cost (throttled `ingest.text`, same envelope as §4 / A2):**

| run | n | p50 | p90 | mean |
|---|---|---|---|---|
| A2 baseline (2026-10-07) | — | **1282.7** | **1295.9** | **1294.7** |
| B3 run B | 4 | 1305.9 | 1323.7 | 1317.2 |
| B3 run 2 | 7 | 1321.8 | 1346.0 | 1335.8 |
| B3 run 1 | 7 | 1335.7 | 1370.6 | 1345.1 |
| B3 run A | 4 | 1357.4 | 1364.3 | 1369.3 |
| B3 run 3 | 7 | 1370.3 | 1380.4 | 1376.6 |

Five clean B3 runs land p50 **1305.9–1370.3** (centre ≈1338) vs the A2 p50
1282.7 — a consistent **≈ +55 ms (+4.3 %)** shift, slightly above the
run-to-run spread (~50 ms).

**The cost is round-trips, not the heartbeat.** A no-transformation
`process_source` now adds **8 fresh-connection MERGE writes** (command
5/10/90/100; graph 10/40/55; heartbeat t=0) **plus 6 cancel SELECTs** (command
92/143/165; graph 218/352/412) — ~14 extra DB round-trips.
`open_notebook/database/repository.py` opens a new `AsyncSurreal`
(signin + use + close) **per `repo_query` call**, so each is a fresh websocket
handshake (~7 ms in-container; 41.5 ms measured host→`localhost:8108`). The
**heartbeat thread itself is one of the 14** (≈0.5 %, the "~0" the locked row
anticipated); the balance is the progress/cancel checkpoints that deliver the
feature. The single 2505 ms outlier in the first measurement was the watchdog's
stale-marking/emission burst (proven by the `06:33:42` logs), not ingest code.

**Verdict: accepted.** A debt/UX fix, not a speedup, so the "strictly positive
delta" flag gate does not apply; the ≈ +4 % is on a flow already over its
1000 ms budget and buys repair of the >24 h stuck `running` source plus visible
progress and cancel. Keep. Two cheaper variants are recorded for a later pass,
not taken now (they would expand the locked checkpoint set): coalescing the
progress write and the cancel read into one `UPDATE … RETURN AFTER`
(≈ −6 round-trips), and dropping the duplicate `progress=10` written by both the
command and `content_process`.

### B2 — deep-retry visibility for `process_source` (2026-10-08)

**Backoff + jitter was already satisfied.** `process_source` runs
`wait_strategy: "exponential_jitter"`, `wait_min: 1`, `wait_max: 120`
(`commands/source_commands.py:56-72`), so no timing change was made. The only
missing half of the locked decision was "a retry-distribution log line so a
deep-queue stall is visible".

**Why it was invisible.** The same command logs every retry at
`retry_log_level: "debug"` ("Avoid log noise during transaction conflicts",
line 71), while the worker *process* runs loguru at INFO —
`surreal_commands.core.worker.configure_logging(debug=False)` does
`logger.remove()` then `logger.add(sys.stderr, level="INFO")`. A command stuck
at attempt 12/15 therefore looked identical to one that was merely slow. The
library hardcodes `before_sleep=create_before_sleep_callback(...)` inside
`build_async_retry_instance`, so there is no decorator hook to inject a custom
callback — the seam has to be app-side.

**Change.** `open_notebook/utils/retry_visibility.py` adds **one extra loguru
handler** (level DEBUG) whose filter passes only *milestone* retry records —
default every 5th attempt — and stamps the running distribution (retries
observed, deepest attempt, next wait) into `record["extra"]` for the format.
It is installed from `commands/__init__.py`, which the worker imports via
`--import-modules commands` *after* `configure_logging`, so the handler
survives. loguru is **not re-entrant** (verified: re-logging from a sink raises
in `_handler.emit`), so the handler only filters/formats — it never calls
`logger.*`. Config: `OPEN_NOTEBOOK_RETRY_VISIBILITY_STEP` (default 5;
`0`/`off`/`none` disables).

**Verified at the worker's effective level (loguru `level="INFO"`):** attempt 5
emits `[Retry] deep retry | … | retries=2 deepest=5 next_wait=8.0s` on stderr,
attempt 3 and a non-retry debug line stay hidden. Parity green — **1043 tests**
(1029 baseline + 14 new), `ruff check` / `format --check`, `mypy .` clean.

**Verdict: keep.** Observability fix, not a speedup, so the "strictly positive
delta" gate does not apply. Deliberately does not flip `process_source` to
`retry_log_level: "warning"` (that would emit all 15 attempts per conflict
storm — the exact noise line 71 exists to avoid); the milestone sampling keeps
a deep-queue stall loud without it.

### B4 — opt-in `OPEN_NOTEBOOK_REQUIRE_AUTH` guard (2026-10-08)

**The footgun.** `PasswordAuthMiddleware` (`api/auth.py`) authenticates only
when `OPEN_NOTEBOOK_PASSWORD` (or `_FILE`) is set; otherwise it is a pass-through.
That is deliberate — the latency probes, the MCP server and every local tool in
this repo call the API unauthenticated — but it means a deployment that *meant*
to have auth and forgot the password comes up identical to one that did not:
wide open on :5055.

**Change.** New `OPEN_NOTEBOOK_REQUIRE_AUTH` (default **off**).
`require_auth_from_env()` accepts `1/true/yes/on` (case-insensitive) and treats
everything else as off (a malformed value logs a warning rather than silently
turning auth *on* against the operator's intent). `enforce_require_auth()` is
called at the top of the API lifespan, **before** the database readiness probe
and migrations, and raises `RuntimeError` with an actionable message when the
flag is on and no password is configured — so the process exits instead of
serving. `auth_is_configured()` uses the same `get_secret_from_env` lookup the
middleware uses, so the guard and the middleware can never disagree.

**Kept default-off.** Flipping it on by default would break the probe harness,
MCP integration and the offline flow; localhost-bind-only was rejected (breaks
compose), and document-only was rejected (the footgun stays). This is exactly
the locked "opt-in guard" decision.

**Verified.** 21 new unit tests (env parsing incl. malformed + `_FILE`,
configured/unconfigured matrix, and a lifespan test proving the guard raises
*before* `AsyncMigrationManager` is constructed). Parity: `ruff` / `mypy` clean.
Live simulation with `OPEN_NOTEBOOK_PASSWORD=` +
`OPEN_NOTEBOOK_REQUIRE_AUTH=true` raises the documented `RuntimeError`.

**Verdict: keep.** Security/debt fix, not a speedup; the "strictly positive
delta" gate does not apply. No behaviour change when the flag is unset.
