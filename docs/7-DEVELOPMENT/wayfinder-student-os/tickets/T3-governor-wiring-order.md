# T3 — Governor Wiring Order (Idle/Hibernate + Lease Guard + Portable Split)

Label: `wayfinder:task` (HITL checklist + AFK wiring). Parent: `../map.md`.
Blocks: nothing. Blocked by: nothing (frontier).

## Question

Do the manual + wiring work that unblocks the efficiency decision: (1) confirm
the lease-state query (surreal-commands job lease/heartbeat fields) the
**lease guard** will check before any auto-stop; (2) order the compose wiring
— idle-stop for whisper/piper/frontend (profile-gated), pressure-throttle for
worker/API (poll-interval + concurrency scale-down), 5min grace, whitelist
surrealdb/worker/api; (3) plumb the portable split (DB local, bulk portable:
`OPEN_NOTEBOOK_DATA_DIR`-style override covering extraction cache
`open_notebook/utils/extraction_cache.py`, trainer artifacts, sidecar models).
Done when the checklist is executed and the resulting facts (lease field
names, compose keys, env names, measured idle RAM reclaimed per service) are
recorded here. No disk-hibernate of worker/DB.

## Findings, lease authority (2026-10-11, read-only inspection)

Single authority: the SurrealDB **`command` table** — no worker-internal state
needed. Verified against primary sources:

- Statuses (`.venv/.../surreal_commands/core/service.py`): `new / running /
  completed / failed / canceled`. Worker sets `running` on claim; it stays
  `running` through retries; terminal states only at the end.
- Liveness fields (`api/job_monitor.py`): `status`, `heartbeat_at`
  (ISO-8601 UTC, written by `Heartbeat` thread + `report_progress` on every
  command path), plus `progress`/`stage`/`error_message`/`args`.
- Existing poll loop: `JobMonitor.run()` every
  `OPEN_NOTEBOOK_JOB_MONITOR_SECONDS` (default 60s); `_fetch_active` =
  `SELECT ... FROM command WHERE status = 'new' OR status = 'running'`;
  `is_stale(row, threshold, now)` with `OPEN_NOTEBOOK_STALE_JOB_SECONDS`
  (default 300s); a `running` job stale on **two consecutive sightings** is
  marked `failed` (`stale: no heartbeat`).
- Lease-guard rule (proposed): block auto-stop while ANY row satisfies
  `status == 'new'` OR (`status == 'running'` AND NOT `is_stale(...)`).
  Stale-`running` must NOT count as active, or a crashed job would veto
  hibernate forever. Reuse `ACTIVE_STATUSES`, `is_stale` directly — the
  governor adds no new polling, it reads on the monitor's loop/tick.

## Graceful-stop contract (2026-10-11, read from installed sources)

- No SIGTERM handler, no drain, no requeue anywhere in
  `surreal_commands/core/worker.py`: `asyncio.run(listen_for_commands(...))`
  cancels all tasks on SIGTERM. In-flight jobs are **abandoned mid-execution**.
- `CancelledError` is a `BaseException`: `service.py execute_command` catches
  `RetryError`/`Exception` only, so the final `update_command_result` never
  runs. The row stays `running` with a frozen `heartbeat_at`.
- Recovery is existing machinery, not new code: JobMonitor marks doubly-stale
  `running` rows `failed` (`stale: no heartbeat`), and failed sources are
  retryable from the UI. Queued `new` rows survive untouched and are picked
  up on next boot ("Checking for existing commands").
- supervisord `[program:worker]` sets no `stopwaitsecs`/`killasgroup`: default
  10s then SIGKILL. When compose wiring lands, the worker needs
  `stopwaitsecs >= 30` so `finally: hb.stop()` blocks can finish.
- Accepted resume story: **job-level retry from the last saved stage**, never
  mid-step restore. A drain wrapper (stop listener, await semaphore with
  timeout) is deferred until abandon churn proves costly.
- UI signaling: the UI already polls `/commands/jobs`; a "draining/
  hibernating" banner needs a worker-state flag — deferred to the compose
  wiring slice, after the wake path exists.
