# T2 — Tier-Gate Spec (Thresholds + Authority)

Label: `wayfinder:grilling` (HITL). Parent: `../map.md`. Blocks: T4, T5.
Blocked by: nothing (frontier).

## Question

Lock the **capability tier** spec: the live metric authority (`MemAvailable`
vs total RAM), exact cutoffs (proposed: <3072MB free or <2 threads →
tiny+tabular only; ≥3072MB free + ≥2 threads → unlock LoRA ≤0.5B), re-check
points (boot + per training request + pre-finetune), ISA input (SSE2 vs AVX2
vs NEON), and the user-facing denial copy below gate. Must define where the
module lives (`open_notebook/utils/` newcomer reusing `resource_tuning.py`
patterns), its interface (one predicate callers learn, e.g.
`tier_allows(feature)`), and how the Next.js UI hides/disables gated features
without breaking existing tests. One measured gate at a time; full battery
green.

## Resolution (2026-10-10)

CLOSED. Predicate implemented as `open_notebook/utils/capability_tier.py`
(`tier_allows`, 9 tests green, commit `e5a3b9a`): per-request MemAvailable +
cpu_count, ≥3072MB + ≥2 threads unlocks LoRA, deny-safe on unknown memory,
ISA log-only. Unblocks T4/T5.
