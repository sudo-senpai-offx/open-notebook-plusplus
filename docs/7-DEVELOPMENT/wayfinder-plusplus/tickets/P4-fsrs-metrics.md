# P4 — Native FSRS queue + metrics dashboard

Label: `wayfinder:task` (HITL). Parent: `../map.md`. Blocks: P6 (export
needs bank+history). Blocked by: P3.

## Question

`py-fsrs` (lazy import, tier-gated) scheduling over the bank with revlog;
desired_retention 0.85 default, per-deck dial later; optimizer deferred
(1000 reviews + 4GB trigger). Study dashboard: retention vs target,
lapses/leech, median s/card, due/forecast/load, streaks, focus minutes —
all derived from event logs, no stored counters. Single-learner scope.
