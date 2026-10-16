# Review outcome and staged revision

- **Status**: Design revised after structured review; Stage 1 approved for implementation
- **Date**: 2026-10-16
- **Reviews**: Skeptic (24 findings, 4 blockers), Constraint Guardian (23 findings, 6 blockers)
- **Relates**: ADR-014, ADR-015, ADR-016, `research-admission-and-clusters.md`

## Verdict

The design as agreed did not survive review. Ten blockers, two of them in
premises the primary designer asserted without verifying. Stages 2 and 3 are
deferred; Stage 1 is rebuilt with the corrections below.

## Errors in the primary designer's own claims

Recorded because they are the most useful part of the review.

| Claimed | Actual |
|---|---|
| "`psutil` is already a dependency, so memory reads are free" | Not installed, not in `pyproject.toml`. The sole import is wrapped in `try/except ImportError`, which proves the opposite |
| "`OLLAMA_NUM_PARALLEL=2` lets the server serve a second request" | Exists in host systemd only, outside the repo, and is not set on the phone (`termux-launch.sh` sets `-np 1`) |
| ADR-015 reuses the sync token for delegation | The sync token is an SSRF and library-exfiltration primitive: `blobtransfer.py` builds request URLs from a caller-supplied `peer` with no host allowlist |
| "The phone makes an HTTP call to the PC's API" | `blobtransfer.py:10` records this as measured false: *"the phone cannot open a TCP connection to the PC — ICMP succeeds, no SYN arrives"* |

Two of these were load-bearing. The first underpinned the memory gate; the
second underpinned the entire argument for never cancelling in-flight work.

## Blockers, grouped

**Missing foundations.** The memory gate needs a `MemAvailable` reader; none
exists in the tree, and `psutil` is absent. On Android `MemAvailable` includes
reclaimable page cache — where model blobs and RocksDB buffers live — so it can
read gigabytes higher than what a process can fault. The measuring instrument
would have reproduced the spiral it exists to prevent.

**`held` breaks existing behaviour.** `_skip_reason` treats any status outside
`new`/`running`/`failed`/`canceled` as "never started, so queue", and is called
from every blob pull and every sync import. Under memory pressure every sync
cycle would re-queue every held job, and each duplicate would eventually run —
manufacturing the RAM pressure that caused the hold. `ensure_insights`
idempotency breaks the same way.

**The gate was per-job, not aggregate.** With 20 GB free and a 6 GB model, three
promotions each pass individually on a 15.3 GB device. Under RAM-only that is an
OOM kill of the largest RSS process, which is Ollama — and because running work
is never cancelled, the job is not stopped before it dies.

**Two reapers, opposite policies.** `JobMonitor` marks stale jobs `failed`
permanently; the design's dispatcher would return them to `held`. Same event,
two outcomes, decided by a race.

**No atomic claim.** The worker reacts to live notifications and writes
`running` with an unconditional merge; any transition to `new` re-fires. Reaping
and re-promoting re-executes the same command row.

**Version skew bypasses the gate.** A new device delegating to an old one hits
`commands/jobs`, which enqueues at `status: "new"` — claimable regardless of the
peer's memory gate.

**Security has no substrate.** Auth is off by default; password and sync token
are single shared scalars; there is no device table or revocation list. Both
devices already serve their API on `0.0.0.0` with `CORS_ORIGINS = *`, no rate
limit, and arbitrary command submission.

**mDNS has three unmet preconditions** on the phone: no foreground service, no
`MulticastLock` (Android 9+ blocks background acquisition), and proot where
`IP_ADD_MEMBERSHIP` is a ptrace round-trip.

## Corrections applied to Stage 1

1. **Footprint is per model, not per job.** Three jobs sharing a model need one
   resident model. The original per-job reading would have wrongly held work
   that was safe.
2. **The dispatcher runs inside the API process**, alongside the existing
   `JobMonitor`, rather than as a sixth unsupervised process.
3. **The dispatcher does not reap.** `JobMonitor` owns staleness. Removing the
   second reaper dissolves the conflict and makes retry an explicit user action
   that already exists.
4. **Memory is read from `/proc/meminfo` directly.** No `psutil`. The gate
   consults both `MemAvailable` and a hard ceiling on projected usage against
   `MemTotal`, because `MemAvailable` alone is optimistic where page cache is
   large.
5. **`held` is added to every existing dedup and skip filter**, so sync and
   `ensure_insights` stop re-queueing held work.

## Staging

**Stage 1 — local admission (building).** `held` state, aggregate model-aware
memory budget, hot throttle, held reasons, status endpoint, source-processing
summary strip. No network, no new credentials, no new attack surface. Verified
on the SDM680 alone.

**Stage 2 — delegation (deferred).** Requires inverting the direction, building
a per-device credential substrate from nothing, and a separate security design.
Its own project.

**Stage 3 — governance (deferred).** mDNS on proot needs a foreground service
and a multicast lock that do not exist. For one operator with two visible
devices, both reviewers independently flagged this as the largest block carrying
the least load.

## Standing finding, independent of this project

Both devices serve their API on `0.0.0.0` with `CORS_ORIGINS = *`, auth off by
default, no rate limiting, and arbitrary command submission via
`POST /api/commands/jobs`. Any host on the network — and any website loaded in a
browser on it — can submit unbounded work. This has nothing to do with
admission control and should be fixed regardless of what happens with
delegation.