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