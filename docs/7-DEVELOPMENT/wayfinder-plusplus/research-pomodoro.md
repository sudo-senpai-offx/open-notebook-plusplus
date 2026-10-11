# Research: Pomodoro / Focus-Session Model for OpenNotebook

Status: read-only research (no code changed). Repo: `/home/dev-sudo-senpai/open-notebook` (Next.js frontend, FastAPI backend, SurrealDB).

## 1. What exists in-repo today

**No pomodoro / focus-timer / study-session code exists.** Verified 2026-10-11 by grep:

- `grep -ri "pomodoro|focus_timer|study_streak|focus_session"` over repo root: **no matches**.
- `grep -ri "pomodoro|timer|focus.*session|study.*session"` over `api/`: **no matches**.
- Same pattern over `frontend/src`: only unrelated matches — `use-ask.ts:83` (stale-connection timer comment), `LanguageLoadingOverlay.tsx` (loading `setTimeout`), `GeneratePodcastDialog.tsx:224` (debounce `setTimeout`). No countdown, interval, or session logic.
- The only "session" concept in `api/` is **chat sessions** (`api/routers/source_chat.py`, `ChatSession` + `refers_to` relation to sources, message counts). No time-boxed focus entity. `api/models.py` (`NotebookCreate/Response`, `Note*`, `Source*`, credentials, delete-cascade counts) has no duration/break/aggregate fields.
- No `focus_sessions` table / `ObjectModel` in `open_notebook/domain/` (entities there: `Notebook`, `Source`, `SourceEmbedding`, `SourceInsight`, `Note` over SurrealDB via `surrealdb.AsyncSurreal` — see `open_notebook/domain/notebook.py`, `open_notebook/database/repository.py`).
- No focus/timer router in `api/routers/` (routers: auth, notebooks, notes, sources, source_chat, insights, search, podcasts, models, providers, settings, governor, etc. — no timer/stats router).
- Closest reusable precedents: **zustand `persist` + `createJSONStorage(() => localStorage)`** is the established client-persistence pattern — used by `theme-store.ts`, `sidebar-store.ts`, `notebook-view-store.ts`, `notebook-columns-store.ts`, `navigation-store.ts`, `auth-store.ts` in `frontend/src/lib/stores/`. Server-persisted precedent is the FastAPI+Pydantic → SurrealDB `ObjectModel` path used for notebooks/notes/sources.

Wayfinder context: project constraint is **offline-first, low-end-first** (Pentium D / 2 GB floor, Snapdragon 439 class ARM; `MINI_ONLY=true` and router/cloud opt-in flags pinned by `tests/test_flag_defaults.py`; Docker-first per `docs/7-DEVELOPMENT/wayfinder-student-os/map.md`). Any timer design must work with the API/DB stopped and must not add a blocking dependency.

## 2. Pomodoro technique — primary parameters

Source of truth: Francesco Cirillo's Pomodoro Technique (pomodorotechnique.com): a **complete system** (plan → focus → reflect) using a timer plus paper tracking — "more than 25 minutes," not just a countdown widget.

Canonical loop (corroborated by Cirillo site, Todoist guide, Duke ARC, Emory library guide, Wikipedia):

1. Pick one task; estimate effort in pomodoros (activity inventory / to-do-today sheet).
2. **Focus 25 min**, uninterrupted. Record interruptions; defer rather than context-switch.
3. **Short break 5 min** (away from screen per Duke ARC).
4. Repeat; after **every 4 pomodoros take a long break of 15–30 min**, then start a new set.

Claims/citations:

- 25-min focus + 5-min break is the canonical interval (Todoist "Pomodoro Technique" guide; Duke ARC "Pomodoro Technique"; Emory Libraries guide; Wikipedia "Pomodoro Technique").
- Long break cadence: every 4 pomodoros, 15–30 min (Todoist; Emory; Duke ARC "Every 4 Pomodoros take a longer 15–30 minute break"; Facebook/GOODHQ summary; rock.so loop description).
- The "record/estimate/compare" step is part of the method, not optional — this is the justification for persisting completed sessions and estimating per-notebook effort (pomodorotechnique.com homepage: "record what happens and improve the way you work").

## 3. Credible variations (configurable presets, not replacements)

| Variation | Parameters | Basis / when to offer |
|---|---|---|
| Classic Pomodoro | 25 focus / 5 break, long 15–30 after 4 | Default. Students, fragmented schedules, low-end attention spans. |
| 50/10 | 50 focus / 10 break | Common study-app preset; ~one class period. Lifestack guide documents it as the standard extension. |
| 52/17 | 52 focus / 17 break | DeskTime telemetry: top-10% most productive users averaged 52 min work + 17 min break (The Atlantic 2014; DeskTime blog "Does the 52-17 rule really hold up?"; Hult; The Muse). Observational, not RCT — label as such. |
| 90/15–20 (ultradian) | 90 focus / 15–20 break | Aligns with ~90-min ultradian cycles; Asian Efficiency and Welltory note 2–3 pomodoros ≈ one ultradian block. Best as opt-in for deep reading/writing, not the default for students. |
| Flowtime | self-chosen start, break when focus fades, log actuals | Smits et al. 2025 (PMC12292963) compare self-regulated/Pomodoro/Flowtime breaks on study experience. Useful for advanced users; log actuals rather than enforcing fixed length. |

Recommendation: ship **25/5 + long-break-after-4 as the default**, with 50/10, 52/17, 90/20 as presets plus custom durations. Keep the set-cycle counter (pomodoros-since-long-break) as first-class state — it is what drives the break rule in every variant.

## 4. How study apps persist timer/session state

Surveyed patterns (Forest, Focus To-Do, Athenify, Anki heatmap/Review-Heatmap, Toggl):

- **Authoritative ticking is client-local.** Forest (60M+ users; grows a tree per focused minute, tree dies if you leave early) and browser-based timers (Pomofocus-style, forestfocustimer.com "100% browser-based, no sign-up") tick with client timers + localStorage/IndexedDB so the countdown survives reload and works offline. Server round-trips per tick are never used (battery/latency cost, fails offline).
- **Completed sessions sync to server as immutable event records.** Focus To-Do pins each 25-min session to a task/project; Toggl-style time entries and Anki review logs aggregate later. The record is written once at completion (or pause/cancel), containing: start/end timestamps, planned vs actual focus seconds, phase (focus/short-break/long-break), linked task/notebook id, completion status.
- **Streaks/stats are derived, never stored as primary state.** Anki's Review Heatmap and Studylitics dashboards compute streaks and daily totals from the event log; Anki issue #4085 discussion treats streak as a derived view. This avoids double-write inconsistency.
- **Active-session recovery** uses a persisted "running session" row (start timestamp + planned duration) so a reload reconstructs remaining time arithmetically instead of trusting an in-memory countdown — the correct pattern for crash/otential hibernate on 2 GB devices.

## 5. Recommended model for OpenNotebook

### 5.1 Timer state machine

Phases: `idle → focus → break(short|long) → idle`, plus `paused` overlay on any active phase. Transition rules:

- `focus` length default **25 min** (configurable preset).
- On focus completion: increment `pomodoros_in_set`; if `% 4 == 0` → `long break (default 20 min, range 15–30)` else `short break (5 min)`.
- Break completion → `idle` (require explicit start of next focus; auto-start is opt-in — matches Cirillo's deliberate-start ethos and avoids runaway timers on shared student devices).
- Interruptions: `pause` (clock stops, resume allowed) vs `cancel` (session logged as incomplete with actual seconds — feeds the "compare estimates with reality" reflection step).

Config surface (persist per user, client-side with server mirror optional): `{ focus_min, short_min, long_min, long_every_n (=4), auto_start_breaks=false, sound/haptic on/off }`.

### 5.2 Persistence choice — offline-first, low-end rationale

**Client-authoritative ticking + localStorage (zustand persist) for active state; append-only server log in SurrealDB for completed sessions.**

- Active timer (`phase`, `ends_at` epoch ms, `remaining` derived, `pomodoros_in_set`, linked `notebook_id`) lives in a new `focus-timer-store.ts` following the existing `theme-store.ts` `persist(createJSONStorage(() => localStorage))` pattern. Rationale: (a) timer must tick with API/DB stopped (offline-first promise, governor may idle-stop containers — see wayfinder `map.md` lease/idle design); (b) zero network per tick → battery/RAM safe on 2 GB / SDM439 floor; (c) survives reload via `ends_at` arithmetic, no heartbeat needed.
- On focus/break completion (and on pause/cancel with actuals), POST one immutable record to FastAPI → SurrealDB `focus_session` table: `{ started_at, ended_at, planned_seconds, actual_seconds, phase, status (completed|cancelled), notebook_id?, note_id?, preset_label }`. Small write, infrequent, retryable when offline (queue in localStorage, flush on reconnect — same shape as existing async-processing patterns in sources/embeddings).
- Why not server-ticked: adds a liveness dependency, fails the offline-first test pin, and costs a websocket/poll per second on machines where the worker is capped at `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1`.
- Why not client-only forever: per-notebook aggregates, cross-device history, and the reflection UI need queryable server data; localStorage is also capped (~5 MB) and per-browser.

### 5.3 Metric aggregates (lightweight dashboard)

Compute server-side (SurrealQL `GROUP BY` / time-bucketed queries) from `focus_session` rows; never store counters as source of truth. Minimum viable dashboard:

1. **Focus minutes/day** — `SUM(actual_seconds WHERE phase=focus AND status=completed)` bucketed by local day, trailing 14/30 days. Primary chart.
2. **Sessions/day** — `COUNT(completed focus)` per day; secondary bar/overlay with (1).
3. **Per-notebook focus minutes + session count** — group by `notebook_id` over trailing 7/30 days; ties timer to OpenNotebook's core entity and powers the "estimate vs actual" reflection per notebook.
4. **Current streak (days with ≥1 completed focus)** + **pomodoros-in-set progress** — derived; streak from distinct-day set, set progress from the client's counter.
5. Supporting (cheap, same table): completion rate (`completed / started`), interruption count (pauses per session, if logged), total breaks taken.

Keep the dashboard to these five; Studylitics/Anki-heatmap precedent shows daily-totals + heatmap + per-subject split covers ~90% of student needs without OLAP machinery.

## 6. Suggested implementation shape (for ticket authors)

- Frontend: `frontend/src/lib/stores/focus-timer-store.ts` (zustand persist), timer hook with `ends_at`-arithmetic + `setInterval(1000)` render, break-rule selector, offline outbox for unflushed session records.
- Backend: `api/routers/focus.py` (`POST /focus-sessions`, `GET /focus-sessions/stats?range=&notebook_id=`), Pydantic `FocusSessionCreate/Response` in `api/models.py`, SurrealDB `focus_session` `ObjectModel` in `open_notebook/domain/` following the `Note`/`Source` pattern.
- Tests: unit (break-rule transitions incl. 4th-pomodoro long break, pause/resume arithmetic), persist/rehydrate round-trip, stats aggregation query; full battery per repo convention (pytest + ruff + mypy).

## 7. Sources

- Primary method: pomodorotechnique.com (Cirillo; "more than 25 minutes"; plan/focus/reflect; free tools page).
- Canonical parameters: Todoist "The Pomodoro Technique — Why it works & how to do it" (25/5; 15–30 after four); Duke ARC "Pomodoro Technique" (5-min off-screen breaks; 15–30 every 4); Emory Libraries guide (4-pomodoro long-break rule); Wikipedia "Pomodoro Technique".
- Variations: Lifestack "The 50/10 Pomodoro Method"; DeskTime blog + The Atlantic (2014) + Hult + The Muse on 52/17 (top-decile telemetry, observational); Asian Efficiency + Welltory on ultradian 90-min cycles; Smits et al. 2025, PMC12292963 (Pomodoro vs Flowtime vs self-regulated breaks).
- App patterns: Forest (forestapp.cc; App Store "used by 60 million people"; grow-tree-per-minute + wither mechanic); Focus To-Do (session-pinned-to-task); Athenify (study tracker + streaks); Anki Review Heatmap / Studylitics (derived streak/heatmap dashboards); Anki issue #4085 (streak as derived view); Toggl (time-entry aggregation); toolfinder.com "Best Study Timer Apps 2026" (Forest-for-focus + Toggl-for-analytics combo pattern).
- In-repo evidence: file:line refs in §1 (`frontend/src/lib/hooks/use-ask.ts:83`, `frontend/src/components/common/LanguageLoadingOverlay.tsx:25`, `frontend/src/components/podcasts/GeneratePodcastDialog.tsx:224`, `api/routers/source_chat.py`, `api/models.py`, `open_notebook/domain/notebook.py`, `frontend/src/lib/stores/theme-store.ts:15-18`).
