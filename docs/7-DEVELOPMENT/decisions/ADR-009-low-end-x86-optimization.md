# ADR-009: Optimizing the existing system for low-end x86 (2 GB floor first)

- **Status**: Accepted
- **Date**: 2026-10
- **Related**: optimize-for-low-end work (next after site crawling); MCP/agent-memory (#878/#693/#973) and content modes (#203) are the deferred follow-on horizon

## Context

The fork's direction is to prove the basics are robust and highly optimized before
adding new capabilities, targeting users who self-host on low-end x86 hardware.
That requires a concrete resource floor to define "low-end", and evidence — not
guesses — about where the current system actually burns resources.

A measurement session on the dev machine (same footprint as a 2 GB-class target)
produced the baseline:

| Component | Idle RSS | Task peak | Cold start → ready |
|---|---|---|---|
| API (`uvicorn api.main`, 1 worker) | ~170 MB | ~170 MB | ~2 s |
| Worker (`surreal-commands-worker`) | ~150 MB | **~234 MB** (ingest+embed 14 chunks) | ~60 s (heavy imports) |
| Frontend (Next.js `node server.js`) | ~107 MB | ~111 MB (SSR) | ~0 ms (standalone) |
| SurrealDB (external container v2) | ~314 MiB (container) | — | — |

Real task timings (local Ollama embedding): text source ingest 0.27 s; embed
1 chunk = 5.79 s; embed 14 chunks = 16.5 s.

Stack working set without tasks: 170 + 150 + 107 ≈ **~430 MB**; mid-task ≈
**~515 MB** (worker absorbs the load, API and node stay flat). Add OS (~150–250 MB),
SurrealDB (~314 MB), Ollama + loaded embedding model, and headroom → the natural
fit is the **2 GB RAM class**.

### Re-measurement (2026-10-07, A2 #1 close-out — live throttled stack)

Envelope at measurement: app 2 GiB / 2c, db 512 MiB / 1c (whisper/piper/piper-ja
unconstrained by compose limits — caveat below). Idle values are two identical
10 s-apart samples (stable working set, not a leak in motion); task peak from a
12-chunk embed through the queue.

| Component | Idle RSS | Task peak | vs original |
|---|---|---|---|
| API (`uvicorn`, VmRSS) | ~878 MiB | ~878 MiB (flat) | 170 MB → **5.2×** |
| Worker (`surreal-commands-worker`, VmRSS) | ~865 MiB | ~865 MiB (12-chunk embed: +8 KB) | 150→234 MB → **~5.8×** |
| Frontend (`next-server`, VmRSS) | ~52 MiB | — (SSR) | 107 MB → 0.5× |
| SurrealDB (container) | ~443 MiB / 512 MiB | — | 314 → ~443 MiB |
| Whisper (container) | ~50 MiB | ~50.9 MiB (idle through podcast run) | new cell |
| Piper (container) | ~4.2 MiB | **~1054.7 MiB** (68 s TTS window, A2 #2) | new cell — TTS peak |
| Piper-JA (container) | ~16 MiB | ~16.4 MiB (idle — English voice) | new cell |

**Container working set (the number the 2 GiB cgroup enforces):** docker stats
app = **1.62 GiB / 2 GiB**; cgroup `memory.current` = 2046 MiB (incl. reclaimable
cache). RSS across api+worker+node ≈ 1.86 GB → the stack **idles at / just under
the envelope ceiling**, leaving ~380 MB headroom for heavy flows.

**A2 #2 podcast baseline (2026-10-07, baseline-only — no gate):** default
config (fast/draft presets OFF), `solo_expert` / 5 segments, ollama
`mistral:latest` (7.2B, host-side unthrottled) + piper `en_US-amy-medium`.
Wall time **2084 s (34.7 min)** — LLM (outline 460 s + transcript 1551 s) =
**96.5%**, TTS (38 clips) = 68 s. Two retries, both 180 s provider
ReadTimeouts (7.2B long generations exceed the read timeout on this host;
retry budget max 3 / 5 s × 2ⁿ / cap 30 s never exhausted). Envelope held:
app container peak **1455 MiB / 2 GiB (71%)**, db 426 / 512 MiB, no OOM, no
restart. Piper is the TTS memory outlier (4.2 MiB idle → **1054.7 MiB** in the
68 s window): the concurrent stack during TTS ≈ app 1.345 + db 0.35 + piper
1.03 ≈ 2.7 GiB — fits the per-container envelope here, but **not a single
2 GB physical box**; TTS is the low-end fit risk. Mid-run worker/api VmRSS
(637.7 / 655.4 MiB) sits *below* idle (865 / 878) — cgroup reclaim; container
working set remains the binding metric. Full timeline, coverage notes and
caveats: perf-audit.md § A2 #2.

**A2 #3 container cold starts (2026-10-07, measure & record):** three back-to-
back `docker restart`s under the envelope (2 GiB / 2c). Worker spawn→ready
**12.7–13.1 s** — the stage-2 target `< 15 s` is **met** on every measured
restart (+14.4–14.8 s from container start incl. supervisord/`uv run`); one
first-restart-after-3 h-idle sample measured 18.9 s (variance, cause not
isolated — recorded, not a steady-state failure). API spawn→ready ≈ 13 s,
import-bound: both processes load the eager command-module graph concurrently
on 2 CPUs while the v29 migration check is a ~0.05 s no-op — so the stage-1/2
`API < 5 s` budget (defined for `measure_resources.sh` spawn mode, host
process scope) is missed by ~8 s when read container-side; the two measurements
are not directly comparable. Frontend has **no build at boot** (prebuilt
standalone, `node server.js` ready 0.39–0.41 s after wait-for-api hands off);
issue→serving ≈ 19.4–20.0 s total, dominated by API readiness. Full table:
perf-audit.md § A2 #3.

**A2 #5 chat turn (2026-10-07, measure & record):** opt-in `chat.turn` flow
added to the probe harness (excluded from the default `all` discovery); ollama
wired into the throttled envelope for the run per the locked decision —
CPU-quota'd to **2 CPUs** matching the app's share (`CPUQuota=200%`, cgroup
`cpu.max=200000 100000`), `MemoryMax` left `infinity` because the default chat
model (aya-expanse:8b, 5.1 GB) cannot fit any low-end-plausible cap —
`throttle.sh`'s declared 768m ollama default is never applied (host systemd
service, not a container) and would OOM the load, not throttle it. Clamp
reverted immediately after the run; standing policy D3 (host-side, uncapped)
unchanged. Under the full envelope (app 2 GiB / 2c, db 512 MiB / 1c, ollama
2c): full turn p50 **9.43 s** (min 4.46 s / max 10.09 s), **0 timeouts** —
guardrail clean, nothing to separate from app latency. The budget row is
first-token `< 2 s`; the non-streaming REST surface cannot isolate first
token, so the full-turn figure is recorded per the locked decision — budget
**unverifiable this round**, recorded not gated. Details: perf-audit.md §
A2 #5.

**A2 #6 fixture corpus (2026-10-07, close-out):** the original defect —
throttled heavy flows resetting the connection because the app exceeded the
old 1280 MiB cap on the 85-source corpus — is closed. Fixture
`notebook:1n4cb4qr744izp5xdo26` (4 sources / 9 chunks) is the throttled
corpus per the locked decision; the 85-source corpus stays for unthrottled +
memory-profile runs. Status check under the live envelope (app 2 GiB / 2c,
db 512 MiB / 1c): `search.vector`, `search.text.scoped`,
`search.text.global`, `ingest.text` all completed on/with the fixture,
**errors 0** (no resets), first-beat cold starts noted (scoped search 22.0 s
max vs 0.72 s p50). Details: perf-audit.md § A2 #6.

Caveats: per-process VmRSS includes shared text pages (double-counted across api
and worker) and freed-but-not-returned pymalloc arenas (creeps with uptime) — the
container-level number is the binding constraint. The growth vs the original
~430 MB is a modded-fork regression from **eager static imports** (docling,
content-core, esperanto, podcast_creator graph import at worker boot — see the
import-graph table below), and is the likely root reason transient
`KeyError`/`Future cancelled` flakes appeared under pressure. The original
~430 MB working set is no longer the live baseline; the envelope program's
stage-4 footprint cuts (lazy-import pass) target exactly this. A2 #2/#5 runs
(podcast, chat wired with ollama into the envelope) proceed from ~380 MB
headroom and are recorded, not gated.

An import-graph investigation found the footprint drags are mostly **static
venv weight, not runtime allocations**:

| Package | Size | Pulled by | Reachable at runtime? |
|---|---|---|---|
| `nodejs_wheel` | 199 MB | `pytubefix` | Lazy only — `_fetch_transcript_pytubefix` fallback in content-core |
| `imageio` + `imageio_ffmpeg` | ~77 MB | `moviepy` | Lazy only — `podcast_creator` imported on first use; moviepy audio-only path |
| `pandas` | 41 MB | content-core (declared) | **Zero `import pandas` in content-core** — no code path found |

## Decision

**Stay on the current 3-process supervisord shape (api + worker + node) with
SurrealDB + Ollama external, and optimize all existing functionality within a
2 GB RAM, 2+ core, SSD-class floor.** No headless-only mode, no vendored
SurrealDB, this round.

The work is staged, each stage behind the same repeatable guard:

1. **Harness (foundation).** A repeatable `scripts/measure_resources.sh` +
   optional CI job asserting the 2 GB envelope: per-process RSS limits
   (API < ~220 MB, worker < ~300 MB idle / ~500 MB task, node < ~150 MB),
   startup time (API < 5 s, worker < 30 s), and a smoke ingest→embed task
   that must complete within budget. The harness is dev/CI-only; it ships
   neither in the runtime image nor in the product image.
2. **Responsiveness.** Lazy-import the heavy modules that currently load eagerly
   in the worker import graph (`esperanto`, `langchain-*`, content-core
   processors, podcast/YouTube fallbacks). Target worker cold start 60 s → < 15 s;
   API stays < 5 s.
3. **Throughput.** Tune worker defaults (`--max-tasks`, embed pipeline chunk
   sizes, SurrealDB pool) within the measured envelope; a task must never push
   the stack past the 2 GB envelope (worker task peak ≈ 234 MB today).
4. **Footprint.** Remove or optionalize only dependencies with demonstrated
   unreachable-at-runtime import graphs: `nodejs_wheel` (199 MB, pytubefix-only),
   `imageio`/`imageio_ffmpeg` (~77 MB, moviepy-only), and `pandas` (41 MB, no
   import found) if its removal survives the full test suite. Keep the
   ADR-007 opt-in posture: optional image profile excludes Chromium/docling/
   crawl4ai, default image remains all-inclusive.
5. **Validation.** Full parity battery (pytest, ruff, mypy; vitest, eslint,
   next build) green + harness green, then this ADR is considered implemented.

**Deferred (unchanged horizon):** MCP server / agent memory (#878/#693/#973)
and content modes & artifacts (#203) come after the low-end plateau is proven.

## Alternatives considered

- **1 GB RAM floor.** Rejected as the first target: reachable only with
  aggressive cuts (dropping node server, vendor-trimmed SurrealDB, external
  Ollama). Keep it as a possible later milestone once the 2 GB class is proven
  green.
- **Headless / API-only deployment mode** (no Next.js node server, ~110 MB
  saved). Rejected this round to protect a single supported deployment shape;
  a headless variant is an easy future add if 1 GB is pursued.
- **Vendoring SurrealDB into one container.** Rejected: changes the support
  surface and diverges from the current external-DB deployments; the same
  principle as ADR-007's incremental opt-in.
- **Multiple small ADRs (one per stage).** Rejected: one ADR-009 documents the
  whole low-end program as a single reviewable decision; stage progress is
  tracked by the harness, not by doc churn.
- **Footprint-first ordering.** Rejected: the import-graph evidence makes
  footprint cuts low-risk, but startup and throughput are what make low-end
  hardware *feel* fast, and the harness must exist before any cut so nothing
  regresses invisibly.

## Consequences

- A regression guard (harness) becomes the definition of "still fits the 2 GB
  envelope" for every future change, development and CI.
- Worker cold-start improvement helps every deployment (dev machines and
  container restarts), not just low-end targets.
- Dependencies are only removed on import-graph + test evidence; anything
  reachable at runtime (e.g., content-core hard deps) stays. The YouTube
  fallback and podcast path remain functional after `nodejs_wheel`/`imageio`
  removal — they are simply not importable until their optional deps are
  installed, matching the ADR-007 opt-in pattern already used for docling and
  crawl4ai.
- MCP/agent-memory and content modes (the #878/#693/#973 and #203 clusters)
  proceed on top of a stable, measured 2 GB baseline rather than on an unknown
  one.