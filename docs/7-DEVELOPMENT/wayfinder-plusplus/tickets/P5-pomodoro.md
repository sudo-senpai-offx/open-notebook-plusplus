# P5 — Pomodoro with session lease

Label: `wayfinder:task` (HITL). Parent: `../map.md`. Blocks: nothing.
Blocked by: nothing (frontier — independent track).

## Question

Client-authoritative timer (zustand persist, ends_at arithmetic; presets
25/5, 50/10, 90/20; set-cycle counter drives long breaks), running-session
DB row for crash/hibernate recovery, start/end registration = hibernate
veto lease (2 calls/session), break banner suggesting due reviews +
conversion metric, optional sticky notebook tag, completed sessions as
immutable events. No per-tick network. Tests + i18n mirror.
