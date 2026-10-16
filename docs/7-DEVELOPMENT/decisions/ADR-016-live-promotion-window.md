# ADR-016: Concurrency is a live promotion window, not a worker setting

- **Status**: Accepted
- **Date**: 2026-10-16
- **Supersedes**: the restart-based throttle shipped in `beef791`
- **Related**: ADR-004 (background workers), ADR-014 (RAM-only runtime)

## Context

Worker concurrency in `surreal_commands` is fixed at process start: the worker
creates `asyncio.Semaphore(max_tasks)` once, before entering its listen loop.
That is what made throttle a restart-only setting, and `beef791` shipped it that
way — a persisted preference read by both launchers at startup, with the worker
recording what it started with so the API could report the truth.

There is no pre-claim hook. The claim is one query,
`SELECT * FROM command WHERE status = 'new' ORDER BY created ASC`, with no
interceptor, filter or predicate surface anywhere in the package. Concurrency is
a semaphore and nothing else.

The requirement is that full throttle apply on the spot, like a switch.

## Decision

Jobs are created in a **`held`** status. A per-device dispatcher promotes them to
`new` — but only up to a **live window size**, and only when memory permits.

The vendor worker is unchanged. It claims whatever is `new`; its `--max-tasks`
becomes a deliberately high backstop that should never bind, because the
dispatcher is the real limit.

Concurrency is therefore the number of jobs the dispatcher is willing to make
claimable, not a semaphore width. Changing it takes effect on the dispatcher's
next pass — no restart, no reconciliation, no pending state.

The window counts `new` **and** `running` together, because the worker flips
`new`→`running` asynchronously; a burst of promotions would otherwise all sit in
`new` and bind the semaphore behind our back.

Submission writes rows directly with `status: "held"`, validating arguments
against the same command registry `submit_command` uses. That helper hardcodes
`status: "new"`, and the worker's live subscription can claim a row before a
subsequent flip, so routing through it would reintroduce a race.

## Consequences

- **Shrinking applies going forward; running work is never cancelled.** A killed
  job strands in-flight work as a stale failure. The cost is a brief window
  where more jobs run than the target, which the UI shows honestly rather than
  pretending the target has been met.
- Interactive work is never cancelled or gated; batch is what the window bounds.
- The dispatcher is a new always-on process with its own failure modes. Its
  restart must be a no-op, and it must reap jobs whose worker died.
- A dispatcher that dies stops promotion entirely — jobs stay `held` rather than
  piling up unrun. Failing toward "nothing runs" is the correct direction: the
  alternative is an ungated queue, which is ADR-014's failure mode.
- **This deletes working code.** `restart_required`, `throttle_restart_pending`,
  `throttle_target`, `throttle.running`, and both launchers' persisted-preference
  reads, along with their tests, become dead. The user directed this: a
  restart-based toggle cannot satisfy "apply on the spot". Recorded so the
  deletion reads as archaeology rather than a mystery.

## Alternatives considered

- **Resizing the vendor semaphore in place.** Smallest diff, but monkeypatching a
  private symbol. It breaks silently on a package upgrade, and here silent means
  "jobs stop running" rather than an exception.
- **Our own worker loop.** Maximum control, including per-task gating. Rejected
  for now: it means reimplementing reconnect, heartbeats, retry and graceful
  shutdown, all of which the vendor already gets right and all of which have
  already caused us a stale-job strand. Worth revisiting if per-task gating
  becomes a real requirement.
- **Keep restart-based throttle.** Rejected by requirement.