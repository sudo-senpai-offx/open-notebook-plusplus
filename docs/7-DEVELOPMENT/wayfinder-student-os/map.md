# Wayfinder Map — Student Ephemeral Learning OS on OpenNotebook

Label: `wayfinder:map`. Local-markdown tracker (no issue tracker configured).
Tickets: children in `./tickets/`. Refer to tickets by name, never bare id.

## Destination

A hardened, Docker-first OpenNotebook that runs ideally on a student's
whatever-device (floor: Pentium D / Athlon 64, SSE2, 2GB; then Snapdragon 439
class ARM) with mini-first inference, tier-gated tiny/LoRA training, and
idle/hibernate efficiency — proven in containers before any portable-app or
bootable-OS release. The way is clear when every ticket below is resolved and
the sequencing into implementation tickets is agreed.

## Notes

- Domain: OpenNotebook (`/home/dev-sudo-senpai/open-notebook`), Docker-first.
  Consult `docs/7-DEVELOPMENT/architecture.md`, `decisions/` (esp. ADR-007
  opt-in runtimes, ADR-009 low-end x86), `perf-audit.md`, `locked-plan-2026-10.md`
  every session. Prior research: `research/00-overview-and-recommendations.md`
  (MiniGPT / qapla / NeuralXP / llama.cpp PR #12871 precedents).
- Standing constraints: local-only commits (no push to `origin` without
  maintainer authorization); offline-first defaults pinned by
  `tests/test_flag_defaults.py` (`MINI_ONLY=true`, router/mixture/reranker/cloud
  opt-in false); `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1`; full battery
  (pytest + ruff check/format + mypy) green per change; one measured change at
  a time; heartbeats must survive every long-blocking path (`stale: no
  heartbeat` churn must not regress).
- Vocabulary: **capability tier** = MemAvailable + thread + ISA verdict gating
  training features; **lease guard** = never auto-hibernate while worker holds a
  leased/active job; **bulk dir** = portable models/cache/training artifacts;
  **DB dir** = local RocksDB (stays local for lease reliability).

## Decisions so far

- [Docker-first stabilization before portable/OS release](tickets/T3-governor-wiring-order.md): harden in containers; portable app + bootable ISO only after approval.
- [LoRA-capable with efficiency optimization](tickets/T4-flag-split-order.md): tiny + tabular + LoRA ≤0.5B, with idle/hibernate to survive 2GB.
- [Web UI first, other stacks later](tickets/T5-tiny-recipe-prototype.md): stabilize existing Next.js UI; Qt/native/TUI deferred.
- [x86 Pentium D floor first, then ARM SDM439](tickets/T1-llamacpp-sse2-sidecar.md): validate against NeuralXP envelope before ARM specifics.
- [Tiny training A→B](tickets/T5-tiny-recipe-prototype.md): fixed tiny recipe first, custom-text import after measured success.
- [Tabular = LightGBM + FLAML, deps installable](tickets/T6-tabular-build-impact.md): storage cheap, portable drives welcome; optimize where needed.
- [LoRA = subprocess + sidecar + 4GB gate](tickets/T2-tier-gate-spec.md): llama.cpp `finetune` isolated (parent heartbeats), PEFT/torch only in opt-in sidecar, exposed only above tier gate.
- [Efficiency = hybrid idle-stop + pressure-throttle](tickets/T3-governor-wiring-order.md): stop media/UI on idle; throttle worker/API under pressure; no disk-hibernate on USB.
- [Governor = hybrid with lease guard](tickets/T3-governor-wiring-order.md): idle + pressure, whitelist surrealdb/worker/api, 5min grace, never stop with leased job.
- [Flags split](tickets/T4-flag-split-order.md): `MINI_ONLY` stays inference-routing; new `ALLOW_TINY_TRAIN` / `ALLOW_LORA` default false, tier-gated; existing test untouched.
- [Inference = opt-in llama.cpp SSE2 sidecar](tickets/T1-llamacpp-sse2-sidecar.md): host Ollama stays default; sidecar (`GGML_NATIVE=OFF`) proves no-AVX path.
- [Tier authority = MemAvailable + threads, per-request](tickets/T2-tier-gate-spec.md): re-checked per training request, not boot-only.
- [Portable layout = split](tickets/T3-governor-wiring-order.md): DB local, bulk (GGUF/adapters/datasets) portable via `OPEN_NOTEBOOK_DATA_DIR`-style override.

## Not yet specified

- Portable-app packaging (zip layout, start scripts) — specifiable after Docker phase proves green.
- Bootable live ISO (antiX/MX + prebuilt stack) as flagship demo artifact.
- ARM SDM439 tuning (bitnet.cpp ternary vs llama.cpp `armv8-a`, NEON INT8 paths).
- UI stack switch (Qt/native/TUI) after web stabilization.
- Exact idle timeouts / pressure thresholds (pending T2 measurements).
- Training UI surface (where train buttons live in current Next.js app).

## Out of scope

- Any push to `origin` without maintainer authorization (local commits only).
- Flipping `MINI_ONLY`/router/mixture/reranker/cloud defaults on (test-pinned offline-first promise).
- GPU-dependent features default-on; big-model (>3B) on-device training claims.
- From-scratch LLM pretraining on low-end (research rules it out; sub-20M tiny only).
- Disk-hibernate of worker/DB on portable USB (I/O-stall + lease risk).
