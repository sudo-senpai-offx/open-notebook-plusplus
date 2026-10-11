# Wayfinder Map — open-notebook-plusplus (Student Learning OS)

Label: `wayfinder:map`. Local-markdown tracker. Tickets: children in `./tickets/`.
Parent effort `../wayfinder-student-os/` (low-end hardening) is prerequisite infra.

## Destination

A credited fork of lfnovo's OpenNotebook (`open-notebook-plusplus`) that is a
complete student learning OS: generate cards/quizzes from sources, retain via
a native FSRS queue, measure via dashboard, sync via `.apkg`/Obsidian flows,
and sustain focus via pomodoro — all offline-first on the 2GB floor. The way
is clear when the v1 ticket set below is resolved in order and the done-list
(Q8-A) ships.

## Notes

- Research (all primary-sourced, spot-verified 2026-10-11): `research-anki.md`
  (AnkiConnect desktop-only; `.apkg` the real channel; never hand-tune FSRS),
  `research-srs.md` (`py-fsrs` MIT, base dep typing-extensions only; card
  rules ONE_FACT/SHORT_A/CLOZE_FIRST/NO_LISTS; SM-2 spec fallback),
  `research-pomodoro.md` (25/5+long/4 default; client-authoritative ticking
  via zustand persist; event-log metrics; running-session row for recovery),
  `research-obsidian.md` (no in-repo code today; two one-way flows first;
  export-only conflicts; additive-safe filenames). Audit spike pending —
  seams below are provisional until it lands.
- Standing constraints (inherited): local-only commits; offline-first flags
  pinned; `WORKER_MAX_TASKS=1`; full battery green; tier-gate heavies;
  lazy-import heavies; personal single-learner scope (no profiles).
- Vocabulary: **system of record** = native FSRS queue (offline); **bank** =
  approved cards; **inbox** = unapproved drafts; **revlog** = review event log;
  **lease** = open focus session (hibernate veto); **tombstone** = deleted
  flag (Anki copies need manual deletion).

## Decisions so far

- Fork: markers + sync policy (Q1-C); docs now, strings at release (Q7-C).
- Order: generate → retain → measure → sync, non-AI finally (Q2-A/C).
- Anki: native record, `.apkg` export, metrics-back optional; no
  scheduling-sync promise (Q3-D corrected).
- Card models frozen v1: Basic + Cloze, fixed IDs, GUID=hash(source+index).
- Obsidian: two one-way flows (watcher ingest, frontmatter export), no
  write-back; filenames embed stable IDs (Q4-A', R4Q2-C).
- Generation: built-in transform prompts now (ONE_FACT/SHORT_A/CLOZE_FIRST/
  NO_LISTS + good/bad examples), dedicated pipeline on measured need (Q5-C).
- Scheduler: `py-fsrs` lazy + tier-gated, **desired_retention 0.85 default**,
  per-deck dial later; optimizer after 1000 reviews + 4GB tier (Q6-C, R5Q4-C).
- Metrics dashboard first, inline later (Q7-C); set: retention vs target,
  lapses/leech, median s/card, due/forecast/load, streaks, focus min (Q8 set).
- Pomodoro: presets (25/5, 50/10, 90/20, custom last), session lease veto
  (register start/end, 2 calls/session), break banner + conversion metric,
  focus↔notebook optional sticky tag (Q8-C, R5Q5/6-C).
- Review sessions time-boxed with visible carryover; leech auto-suspend at 8
  + inbox (R5Q1/2-C); 20 new/day, per-deck later (R5Q3-C).
- Due awareness: badge + ntfy digest (R5Q7-C); event-log retention:
  export-then-prune at 365d (R5Q7-C twin).
- `.apkg` manual-first (R5Q5-A); sharing = `.apkg` file (R4Q3-C); deletes =
  tombstone + honest copy (R4Q8-C); CSV = Anki TSV strict subset, extras as
  tags (R5Q8-C); streaks only, no gamification (R4Q5-A); no exam mode (R4Q1-A).
- i18n per-slice mirrored keys (R4Q6-C); text-first cards, media lazy (R4Q7-A).

## Not yet specified

- Exact transform-prompt texts (seed in research-srs §4 — adapt, don't invent).
- Revlog/card SurrealDB schema (await audit seam confirmation).
- Approval-inbox + Review-section component shapes (await audit).
- ntfy digest emission point (governor daily edge vs API cron — decide in slice).
- Reconciliation patch if the audit contradicts any seam above.

## Out of scope (v1)

- Bidirectional Obsidian sync; Anki scheduling sync; exam mode; gamification
  beyond streaks; FSRS optimizer; custom pomodoro durations; multi-profile;
  in-app deck sharing; mid-step training resume (prior effort).
