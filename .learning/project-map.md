# Project map

## Purpose
Open Notebook — open-source, privacy-focused research assistant (NotebookLM
alternative): ingest sources, chat/ask over them, generate transformations,
podcasts, embeddings. Three tiers, fully self-hostable, offline-first.

## Requirements
- Low-end envelope: 2 GiB app / 2c, 512 MiB DB, `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1`.
- Offline-first defaults pinned by `tests/test_flag_defaults.py`.
- No push to `origin` without maintainer authorization; full battery green per commit.
- Student-OS goal: ideal function on whatever device (Pentium D floor → SDM439 ARM).

## Components
| Component | Responsibility | Status | Files |
| --- | --- | --- | --- |
| frontend | Next.js UI (:8502) | implemented | `frontend/` |
| api | FastAPI (:5055), routers, migrations on startup | implemented | `api/` |
| worker | surreal-commands worker, async jobs | implemented | `commands/`, `supervisord.conf` |
| source graph | extraction (content-core) + save + transforms | implemented | `open_notebook/graphs/source.py` |
| transformation graph | LLM insight generation | implemented | `open_notebook/graphs/transformation.py` |
| model policy/provision | MINI_ONLY, mini-model routing | implemented | `open_notebook/ai/model_policy.py`, `open_notebook/ai/provision.py`, `open_notebook/utils/model_router.py` |
| job control | heartbeats, progress, OOM helpers | implemented | `open_notebook/utils/job_control.py` |
| resource tuning | thread/env caps at startup | implemented | `open_notebook/utils/resource_tuning.py` |
| extraction cache | on-disk content-hash cache | implemented | `open_notebook/utils/extraction_cache.py` |
| subprocess isolation | heavy-op subprocess wrapper (unwired) | implemented | `open_notebook/utils/subprocess_utils.py` |
| retry visibility | milestone retry log handler | implemented | `open_notebook/utils/retry_visibility.py` |
| tier gate | MemAvailable+threads training gate | chosen | `open_notebook/utils/capability_tier.py` (to build) |
| governor | idle-stop + pressure-throttle + lease guard | ? | — |
| tiny trainer | sub-20M from-scratch recipe | ? | — |
| tabular | LightGBM + FLAML (not installed) | ? | — |
| sse2 sidecar | no-AVX llama.cpp inference backend | ? | — |

## Main flow
UI (:8502) → API (:5055) → SurrealDB (:8108) → worker (max-tasks=1) →
source_graph [extract → save → embed?] → transform_graph [provision mini model → generate]
Training flow: ? (no trainer exists; tier gate → subprocess/sidecar → artifacts in bulk dir ?)

## Decisions
- 2026-10-10 · chosen · Docker-first stabilization before portable/OS release.
- 2026-10-10 · chosen · Efficiency = hybrid idle-stop + pressure-throttle with lease guard.
- 2026-10-10 · chosen · Flags split: MINI_ONLY stays inference; ALLOW_TINY_TRAIN/ALLOW_LORA default false, tier-gated.
- 2026-10-10 · chosen · LoRA via isolated subprocess + opt-in sidecar, 4GB-tier gate only.
- 2026-10-10 · chosen · Inference: keep host Ollama default; opt-in SSE2 llama.cpp sidecar.
- 2026-10-10 · chosen · Tier authority: MemAvailable + threads, re-checked per request.
- 2026-10-10 · chosen · Portable layout split: DB local, bulk portable.

- 2026-10-10 · chosen · T2 tier-gate spec approved: per-request MemAvailable + cpu_count, ≥3072MB + ≥2 threads unlocks LoRA, ISA log-only, backend gate before UI.

- 2026-10-11 · chosen · T4 flags + gate verdict: ALLOW_TINY_TRAIN/ALLOW_LORA default false, gate_training() flag-wins-off then tier verdict, denial names fallback, no auto-routing (commit e7e2068).
- 2026-10-11 · chosen · T3 lease-guard predicate: blocking_leases() + hibernate_allowed(), ACTIVE_STATUSES/is_stale moved to job_control as single definition (commit 824177d).
- 2026-10-11 · chosen · T3 shutdown design locked: stop order media -> worker -> api -> surrealdb last -> frontend self-exit; draining flag while API alive, unreachable-after-flag = graceful; autorestart=false in hibernate mode; wake path deferred.
- 2026-10-11 · chosen · T3 flag home two-tier: .env holds HIBERNATE_* policy (boot-read), live draining signal in SurrealDB service-state record (boot-cleared).
- 2026-10-11 · chosen · T3 hibernate policy plumbing: HIBERNATE_ENABLE/IDLE_SECONDS/STOP_MEDIA inert flags pinned + documented (commit 9492260).
- 2026-10-11 · chosen · T3 service-state record: open_notebook:service_state with draining + boot-clear in API lifespan, no migration needed (commit 662a6d7).
- 2026-10-11 · chosen · T3 config draining field: GET /api/config exposes draining fail-open, no-leak lock extended (commit 38f9507).
- 2026-10-11 · chosen · T3 observe-only governor: api/governor.py evaluate+dormant/vetoed/active/would_hibernate, DB-visible idle, stops nothing, lifespan wiring deferred (commit 6afa18e).
- 2026-10-11 · chosen · Governor live in API lifespan (observe-only, commit 0581623). Next: T1/T6 spikes, T5 prototype, stabilisation.
- 2026-10-11 · chosen · T1 correction: transform routes to chat mini on sub-3GB tiers, 3B kept above gate (commit d886f62). Next: T7 %-allotment spec, dep pins with T5.
- 2026-10-11 · chosen · T7 foundation: percent-of-MemTotal detection + utilization + WARN_RSS_PCT/WARN_ONLY policy, warn-only, unwired (commit 2e7cbaf).
- 2026-10-11 · chosen · T7 pressure note: governor tick appends warn-only RSS% line, action unchanged (commit 6a8df6f). Next: T5 prototype + dep pins, stabilisation.
- 2026-10-11 · chosen · Tabular pins (FLAML dropped: xgboost drags nvidia-nccl-cu12; dGPU optional-future): lightgbm 4.6.0 + sklearn/pandas/scipy floors, lock clean of xgboost/flaml, import RSS 159MB measured, lazy guard pinned (commit 47ea5ba).
- 2026-10-11 · chosen · T5-a1 trainer core: open_notebook/training/tiny.py gated lazy-torch capped runs, smoke-tested (commit 7099516). Next: a2 isolation, UI banner.
- 2026-10-11 · chosen · T5-a2 isolation: tiny_worker CLI + run_tiny_train_isolated(), child crash never kills worker, timeouts raw/transient (commit ddd020f). Next: worker command wiring, UI banner.
- 2026-10-11 · chosen · T5 worker command: train_tiny text-in/stats-out, heartbeats bracket isolation, permanent denials vs transient failures (commit 18d3005). Next: (b) UI draining banner.
- 2026-10-11 · chosen · UI draining banner: types + mapping + non-blocking ConnectionGuard banner, 180 frontend tests green, build clean (commit 9c22a4b). Next: training UI (b2), action governor+wake, sidecar, stabilisation.
- 2026-10-11 · chosen · Training section UI: /training route + job submit/poll/denial, sidebar Create entry, all 14 locales keyed (English mirror), 182 frontend tests green, build clean (commit 8f64181). Next: action governor+wake, sidecar, stabilisation.
- 2026-10-11 · stabilisation live: backend 1151 + mypy-207 clean, frontend 182 + tsc + build clean; live stack (surrealdb/API/worker) drains real backlog; train_tiny E2E submitted, completing via FIFO; launch script ~/bin/open-notebook.sh gains --sse2.
- 2026-10-11 · P1 live: FORK.md + README banner + sync policy (commits a772f1b/58992e5); pre-push secret sweep CAUGHT tracked .env with live key (my c8dd23f) — purged from history via filter-branch, file restored+ignored, both old commits unreachable; public repo github.com/sudo-senpai-offx/open-notebook-plusplus created, main pushed, upstream tracking moved to plusplus/main (bare push can no longer hit lfnovo origin).
- 2026-10-11 · chosen · Governor sleep/wake: unix socket, draining-backed worker stop, rollback on failure, authed wake/state endpoints (commit 90aaebf). Next: sidecar build, stabilisation.
- 2026-10-11 · P2 shipped: card-prompt library (cloze/basic/MCQ + machine formats) with list/install endpoints (commit 9401b34, pushed).
- 2026-10-11 · P5 lease shipped: migration 30 focus_session + endpoints + governor veto (commit 0170c4f, pushed).
- 2026-10-11 · P5 timer UI shipped: /focus route + countdown/presets/log, static i18n key maps, 195 frontend green (commit 06c169c, pushed).
- 2026-10-12 · P4 storage shipped: migration 32 live at v32, image rebuilt and verified current (imports ok, train_tiny registered, 30-32 registered), pushed.
- 2026-10-12 · P8 closed: prune job + daily digest shipped; full-tree sweep green (backend 1227, mypy-245, ruff; frontend 214 + tsc/lint/build); all pushed. Plusplus v1 ticket set complete.
- 2026-10-12 · N1a baseline: floor-flags llama-bench pp512 7.83 / tg128 6.84 @2t (Qwen-0.5B-Q2_K), recorded in research appendix. Next: N1b contenders.
- 2026-10-12 · B5 shipped: own-budget retry loop (15x DB conflicts, 3x transients, permanent immediate), library at 1 attempt; parametrized context-length test split per command (commit 764e701, unpushed — keyring).

## Unknowns
- Lease-state query fields for the governor's lease guard (T3).
- Exact tier cutoffs locked per T2 spec; revisit after T5 prototype measurements.
- llama.cpp SSE2 recipe specifics (T1); LightGBM/FLAML build impact (T6).
- Training UI surface in Next.js app (?).
