"""The dispatcher: what decides a job may start.

Where it runs
-------------
Inside the API process, as a background task alongside the existing
`JobMonitor`. That is a correction to the reviewed design, which proposed a
sixth standalone process. On the phone every process in `termux-stack.sh` is
started with `nohup ... &` and there is no supervisor, no `Restart=always` and
no health check, so a standalone dispatcher that crashed stayed dead until an
operator noticed - which is indistinguishable from a device that is busy.
`JobMonitor` already runs exactly this way, so the dispatcher inherits a
lifecycle that exists and is already known to survive.

What it does *not* do
---------------------
**It does not reap.** `JobMonitor` already marks stale jobs `failed`,
permanently, and emits the user-visible event. A second reaper with the
opposite policy - returning them to `held` - was a blocker: the same event
produced two different outcomes decided by a race, and whichever lost left the
operator with the wrong instruction. Removing the dispatcher's reaper dissolves
that, and makes retry the explicit user action that already exists via
`POST /processing/retry-failed`.

**It does not cancel.** Shrinking the window applies going forward, as decided.
A job in flight is left to finish.

**It does not execute anything.** Promotion is a status change from `held` to
`new`, which is exactly the state the vendor worker already claims. The
worker's own code is untouched, and its `--max-tasks` semaphore is a backstop
that should never bind because the dispatcher is the real limit.

The pass
--------
1. Read the live window (throttle) - a device-local file, read fresh, so a
   toggle applies on the next pass with no restart.
2. Read `/proc/meminfo`.
3. Count jobs currently in `new` **or** `running`.
4. Promote held jobs oldest-first while the window and the memory gate allow,
   recomputing the committed footprint after **each** promotion.

Step 4's recomputation is the whole point. Re-reading the committed set once
per pass and admitting until it stopped fitting is how three individually
valid promotions overshot the device.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.ai.models import JobCheckpoint
from open_notebook.residency import (
    client as residency,
)
from open_notebook.scheduler.footprint import (
    FootprintEstimate,
    JobFootprint,
    job_footprint,
)
from open_notebook.scheduler.gate import evaluate
from open_notebook.scheduler.memory import MemoryReading, current_memory

HELD = "held"
NEW = "new"
RUNNING = "running"
IN_FLIGHT = (NEW, RUNNING)

DEFAULT_WINDOW = 3
MIN_WINDOW = 0
MAX_WINDOW = 8
DEFAULT_INTERVAL_SECONDS = 5.0


def device_cap() -> int:
    """This device's hardware ceiling.

    8 on the PC; the phone launcher exports `OPEN_NOTEBOOK_WINDOW_CAP=2`
    because an 8 GB device with two model servers beside the worker will OOM
    past that. It lives with the device that needs it rather than as a
    constant in the code, because the two devices genuinely differ.

    Read on every call rather than cached at import. The environment does not
    change during a process's life, so caching would be faster - but it would
    also mean the value could not be altered for a test without reimporting
    the module, and a module-level constant named `DEVICE_CAP` reads as
    something fixed when the thing that varies is exactly this.
    """
    raw = os.getenv("OPEN_NOTEBOOK_WINDOW_CAP", str(MAX_WINDOW))
    try:
        value = int(raw)
    except ValueError:
        # A corrupt cap falls back to the ceiling rather than stranding the
        # device with an impossible window.
        return MAX_WINDOW
    return max(0, min(MAX_WINDOW, value))


THROTTLE_KEY = "OPEN_NOTEBOOK_WORKER_MAX_TASKS"


def _throttle_file() -> Path:
    """Where this device keeps its live window size.

    One file, read on every pass - which is what makes the toggle hot. The
    earlier design wrote the preference here and read it once at worker
    start, which is why it needed a restart, a second file recording what the
    worker had actually started with, and a pending flag to reconcile the two.
    A value read fresh each pass needs none of that.
    """
    override = os.getenv("THROTTLE_FILE")
    if override:
        return Path(override)
    if Path("/.dockerenv").exists():
        return Path("/app/data/throttle.conf")
    return Path(__file__).resolve().parents[2] / ".env"


def read_window() -> int:
    """The live window size, or the default.

    Only a bare `KEY=N` with 0-8 is honoured. Anything else - a comment, a
    quoted value, a hand-edit typo, a shell fragment - is ignored rather than
    obeyed, because a corrupt file that is *executed* is a far worse failure
    than one that is ignored.
    """
    import re

    default = min(DEFAULT_WINDOW, device_cap())
    try:
        text = _throttle_file().read_text(encoding="utf-8")
    except OSError:
        return default
    found = default
    for line in text.splitlines():
        match = re.fullmatch(rf"{THROTTLE_KEY}=([0-8])", line)
        if match:
            found = int(match.group(1))
    # Clamped on read as well as on write, so a file written by an older
    # build - or hand-edited to a number this device cannot honour - is
    # reduced rather than obeyed.
    return max(MIN_WINDOW, min(device_cap(), found))


def write_window(value: int) -> int:
    """Persist the window. Returns what was written.

    Only the one key is rewritten; every other line in the file is preserved
    byte for byte. The repository `.env` holds the sync token beside it, and an
    endpoint that rewrites a whole file to store one integer would destroy it.
    """
    value = max(MIN_WINDOW, min(device_cap(), int(value)))
    path = _throttle_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not line.strip().startswith(f"{THROTTLE_KEY}=")]
    kept.append(f"{THROTTLE_KEY}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
    temporary.replace(path)
    return value


@dataclass
class DispatchOutcome:
    """What one pass did, for the status endpoint and the logs."""

    promoted: List[str] = field(default_factory=list)
    held_reasons: Dict[str, int] = field(default_factory=dict)
    window: int = DEFAULT_WINDOW
    in_flight_before: int = 0
    in_flight_after: int = 0
    reserved_bytes: int = 0
    projected_bytes: int = 0
    memory: Optional[MemoryReading] = None
    error: Optional[str] = None
    #: In-flight jobs per workload class, for the status panel. Empty when
    #: residency control is off, which is the only honest value then - the
    #: classes exist regardless, but nothing is counting them.
    role_counts: Dict[str, int] = field(default_factory=dict)
    #: Roles this pass asked the supervisor to load, and how it answered.
    residency: Dict[str, Any] = field(default_factory=dict)

    @property
    def promoted_count(self) -> int:
        return len(self.promoted)


#: Threads each workload class wants while a job of it is in flight.
#:
#: Read from the supervisor's catalogue rather than stated here, because the
#: supervisor derives them from the device's actual topology and the launchers
#: start the servers with those same numbers. A copy in this file would be a
#: third answer to "how many threads does chat use", and it would be the one
#: nothing else is obeying.
#:
#: None means unknown - no supervisor, or an unreadable topology - and then no
#: CPU limit is enforced at all, which is the safe direction: it holds nothing
#: on the strength of a guess.
DEFAULT_ROLE_THREADS: Dict[str, int] = {"chat": 2, "embed": 2}


def role_threads(
    policy: Optional[Dict[str, residency.RolePolicy]],
) -> Dict[str, int]:
    """Threads per class, from the supervisor."""
    if policy is None:
        return {}
    found: Dict[str, int] = {}
    for role in policy.values():
        found[role.role] = role.threads
    return found


async def _fetch_jobs() -> Dict[str, List[Dict[str, Any]]]:
    """Held and in-flight jobs, split by what the gate needs from each.

    `held` carries `created` so oldest-first is real ordering. `new` and
    `running` are needed only to compute what is already resident - a
    completed or failed job's footprint is not charged to anything.
    """
    from open_notebook.database.repository import repo_query

    held = (
        await repo_query(
            "SELECT id, name, args, created FROM command "
            f"WHERE status = '{HELD}' ORDER BY created ASC"
        )
        or []
    )
    live = (
        await repo_query(
            "SELECT name, args FROM command WHERE status IN $live",
            {"live": list(IN_FLIGHT)},
        )
        or []
    )
    return {"held": list(held), "live": list(live)}


def _footprint_of(row: Dict[str, Any], generation_bytes: Optional[int]) -> JobFootprint:
    return job_footprint(str(row.get("name") or ""), generation_bytes=generation_bytes)


def committed_footprint(
    live: List[Dict[str, Any]], generation_bytes: Optional[int]
) -> FootprintEstimate:
    """What the in-flight jobs already require.

    Distinct models, merged - three jobs on one model contribute one entry.
    """
    parts = [_footprint_of(row, generation_bytes) for row in live]
    estimate = FootprintEstimate()
    for part in parts:
        estimate = estimate.merge(FootprintEstimate(models=dict(part.models)))
        if part.uncertain:
            # An in-flight job whose cost is unknown is charged as unknown,
            # which makes every further promotion conservative rather than
            # optimistic.
            estimate.models.setdefault("__unknown__", 0)
    return estimate


async def generation_model_bytes() -> Optional[int]:
    """On-disk size of the generation model, from the local Ollama.

    Returns None when it cannot be determined, which makes generation jobs
    `unknown-footprint` and therefore held. That is the safe direction: a
    wrong guess about a 5 GB model is an OOM, while holding costs only time.
    """
    try:
        import httpx

        base = (
            os.getenv("OPEN_NOTEBOOK_OLLAMA_API_BASE")
            or os.getenv("OLLAMA_API_BASE")
            or "http://localhost:11434"
        ).rstrip("/")
        response = httpx.get(f"{base}/api/tags", timeout=2.0)
        response.raise_for_status()
        models = response.json().get("models") or []
    except Exception:  # noqa: BLE001 - absence is a hold, not a crash
        return None

    sizes = [m.get("size") for m in models if isinstance(m.get("size"), int)]
    if not sizes:
        return None
    # The largest resident model is the one a generation job would need. An
    # under-estimate here is an OOM, so this takes the maximum rather than
    # guessing which tag the configured model uses.
    return max(sizes)


async def _record_hold(command_id: str, reason: str) -> None:
    """Persist why a job is being held.

    Best effort by design: failing to annotate a held job is a cosmetic
    loss in the status panel, and raising here would abandon the rest of the
    pass over a write that changed nothing. A held job that cannot say why is
    still correctly *held* - which is the safety-critical part - it just
    explains itself less well.
    """
    from open_notebook.database.repository import ensure_record_id, repo_query

    try:
        await repo_query(
            "UPDATE $id SET held_reason = $reason",
            {"id": ensure_record_id(command_id), "reason": reason},
        )
    except Exception as exc:  # noqa: BLE001 - annotation is not admission
        logger.debug(f"could not record hold reason for {command_id}: {exc}")


def _roles_of(row: Dict[str, Any], generation_bytes: Optional[int]) -> List[str]:
    """The workload classes one job needs served."""
    return residency.roles_for_models(_footprint_of(row, generation_bytes).models)


def _role_counts(
    live: List[Dict[str, Any]], generation_bytes: Optional[int]
) -> Dict[str, int]:
    """How many in-flight jobs each class currently has."""
    counts: Dict[str, int] = {}
    for row in live:
        for role in _roles_of(row, generation_bytes):
            counts[role] = counts.get(role, 0) + 1
    return counts


def _cpu_committed(live: List[Dict[str, Any]], threads: Dict[str, int]) -> int:
    """Thread-holdings of in-flight jobs, one entry per class."""
    counts = _role_counts(live, None)
    total = 0
    for role, count in counts.items():
        total += count * threads.get(role, 0)
    return total


def _order_held(
    held: List[Dict[str, Any]],
    policy: Optional[Dict[str, residency.RolePolicy]],
    generation_bytes: Optional[int],
    resumed_ids: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Order the held queue so interactive work goes first.

    Three tiers, and only the middle one depends on the supervisor:

    0. **Work needing no model** - 64 MB, finishes in seconds, and unblocks
       whatever queued it.
    1. **Model work, by class priority** ascending - chat at 10, embedding at 50.
    2. **Oldest-first within a tier**, unchanged, because a queue that reorders
       equal work is a queue that can livelock.

    Tier 0 is *not* gated on residency, and that is the fix. Gating it meant a
    deployment with residency off - which is every PC, and the phone before the
    guest enables it - fell back to strict creation order, so the insight write
    a transformation had just finished sat behind every older transformation.
    Measured on a 45-job regeneration: three completed transformations whose
    text was ready to write were queued 40th, 41st and 42nd of 42, about three
    and a half hours behind work that could not have been unblocked by them at
    all. Nothing about tier 0 needs a supervisor - it reads the command name
    off the row - so gating it bought nothing and cost the whole dependency
    chain.

    Tier 1 still requires the supervisor, because class priorities are the
    supervisor's to publish. With no policy every model job lands in tier 1 with
    the same priority, which is exactly strict oldest-first among them - the
    behaviour that test has always pinned.

    This deliberately breaks an invariant the loop below relies on: that a
    refused job means nothing behind it has a better claim. That reasoning is
    only sound under a single total order. Once classes are ordered by priority
    a later chat job legitimately outranks an earlier embedding job - which is
    the entire point of the feature - so the per-row `continue` below now skips
    one job rather than abandoning the queue behind it.
    """

    # Command-name prefixes for prioritization within the resumed tier.
    # User-specified order: embedding > sources > notes > other.
    _RESUMED_PRIORITY = {
        "embed": 0,   # embed_source, embed_note, embed_insight, rebuild_embeddings
        "process": 1, # process_source, crawl_site
        "note": 2,    # any note-related command
    }

    def _resumed_priority(name: str) -> int:
        lower = (name or "").lower()
        for prefix, prio in _RESUMED_PRIORITY.items():
            if lower.startswith(prefix):
                return prio
        return 99  # other commands

    _resumed = resumed_ids or set()

    def sort_key(row: Dict[str, Any]) -> tuple[int, int, int, str]:
        created = str(row.get("created") or "")
        name = str(row.get("name") or "")
        cmd_id = str(row.get("id") or "")
        roles = _roles_of(row, generation_bytes)

        # Tier -1: resumed jobs go first, prioritized by command type.
        is_resumed = cmd_id in _resumed
        tier = -1 if is_resumed else 0

        if not roles:
            return (tier, 0, 0, created)
        if not policy:
            # Equal priority for every model job, so this is oldest-first
            # among them - but still behind the no-model tier.
            resumed_prio = _resumed_priority(name) if is_resumed else 99
            return (tier, resumed_prio, 0, created)
        known = [policy[role].priority for role in roles if role in policy]
        resumed_prio = _resumed_priority(name) if is_resumed else 99
        return (tier, resumed_prio, min(known) if known else 0, created)

    return sorted(held, key=sort_key)


async def dispatch_once(
    *,
    generation_bytes: Optional[int] = None,
    lookup_generation: bool = True,
    memory: Optional[MemoryReading] = None,
    window: Optional[int] = None,
) -> DispatchOutcome:
    """One pass. Idempotent: running it twice with the same inputs promotes
    nothing the second time, because promoted jobs are no longer `held`.

    `lookup_generation` exists because `None` is a meaningful value here, not
    just "not supplied": it means the model size could not be determined, and
    generation jobs must then be held as `unknown-footprint`. Letting `None`
    also mean "go and look it up" meant a caller who genuinely had no size
    silently got one - and a test asserting the hold path passed against the
    live model instead.
    """
    from open_notebook.database.repository import ensure_record_id, repo_query

    target_window = read_window() if window is None else window
    reading = current_memory() if memory is None else memory
    outcome = DispatchOutcome(window=target_window, memory=reading)

    try:
        jobs = await _fetch_jobs()
    except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
        outcome.error = f"could not read queue: {exc}"
        logger.warning(outcome.error)
        return outcome

    held_rows = jobs["held"]
    live_rows = jobs["live"]
    committed = committed_footprint(live_rows, generation_bytes)
    outcome.reserved_bytes = committed.total
    outcome.in_flight_before = len(live_rows)

    if lookup_generation and generation_bytes is None:
        generation_bytes = await generation_model_bytes()

    in_flight = len(live_rows)

    # Per-class policy, from the supervisor. Absent when residency control is
    # off or the supervisor cannot be reached, and absent means "enforce no
    # per-class limits" rather than "fall back to a local copy of the numbers" -
    # a stale copy would hold work for limits that no longer exist, with no way
    # to tell why.
    policy = await residency.policies()
    counts = _role_counts(live_rows, generation_bytes) if policy else {}
    outcome.role_counts = dict(counts)

    # Pre-load checkpoint state so resumed jobs (interrupted by device
    # restart and re-queued as `held`) are promoted first. A checkpoint
    # with non-empty data means the job was mid-execution when the device
    # went down.
    resumed_ids: set[str] = set()
    try:
        for row in held_rows:
            cmd_id = str(row.get("id") or "")
            cp = await JobCheckpoint.get_for_command(cmd_id)
            if cp is not None and bool(cp.checkpoint):
                resumed_ids.add(cmd_id)
    except Exception:  # noqa: BLE001 - checkpoint pre-load is advisory only
        pass

    # Ordering is applied unconditionally. `_order_held` sorts model work by
    # class priority only when there is a policy to sort by, and puts no-model
    # work first either way — that tier reads the command name off the row and
    # needs no supervisor.
    #
    # This call used to sit behind `if policy:`, which made the ungating inside
    # `_order_held` unreachable on the PC: the function was never called at all.
    # A unit test that called `_order_held` directly passed against a change
    # production could not execute, which is why the first attempt at this fix
    # shipped green and changed nothing.
    held_rows = _order_held(held_rows, policy, generation_bytes, resumed_ids)

    # Shared CPU budget, fetched once like the policy. None means unknown - no
    # supervisor or an unreadable topology - and then nothing is enforced.
    cap = await residency.cpu_budget() if residency.enabled() else None
    threads = role_threads(policy) if policy else {}
    committed_cpu = _cpu_committed(live_rows, threads)

    for row in held_rows:
        roles = _roles_of(row, generation_bytes)

        # A class at its concurrency limit is held, never queued behind
        # itself. This is the "deprioritise, never kill" policy: the job in
        # flight is left to finish, no process is signalled, and the newcomer
        # waits for a slot rather than displacing it.
        if policy:
            saturated = next(
                (
                    role
                    for role in roles
                    if role in policy
                    and counts.get(role, 0) >= policy[role].max_concurrent_jobs
                ),
                None,
            )
            if saturated is not None:
                reason = f"class-limit:{saturated}"
                outcome.held_reasons[reason] = outcome.held_reasons.get(reason, 0) + 1
                await _record_hold(str(row.get("id")), reason)
                continue

        # Load on demand. The model server lives outside proot and this process
        # cannot start it, so it asks. A refusal is a hold: the job is not
        # promoted to run against a model that is not there.
        if residency.enabled():
            refused: Optional[str] = None
            for role in roles:
                result = await residency.ensure_resident(role)
                outcome.residency[role] = {
                    "ok": result.ok,
                    "loaded": result.loaded,
                    "pid": result.pid,
                    "model": result.model,
                    "error": result.error,
                }
                if not result.ok:
                    refused = result.hold_reason()
                    break
            if refused is not None:
                outcome.held_reasons[refused] = outcome.held_reasons.get(refused, 0) + 1
                await _record_hold(str(row.get("id")), refused)
                continue

        decision = evaluate(
            window=target_window,
            in_flight=in_flight,
            committed=committed,
            candidate=_footprint_of(row, generation_bytes),
            memory=reading,
            cpu_committed=committed_cpu,
            cpu_candidate=sum(threads.get(r, 0) for r in roles),
            cpu_budget=cap,
        )
        if not decision.admit:
            outcome.held_reasons[decision.reason] = (
                outcome.held_reasons.get(decision.reason, 0) + 1
            )
            # Written back to the row, not just to this pass's outcome. The
            # status endpoint reads `held_reason` from the rows, so a reason
            # that lives only in memory is invisible to the UI - and "we know
            # why this is stuck" was the entire point of the field.
            await _record_hold(str(row.get("id")), decision.reason)
            # Every remaining row is held for at least as long as this one:
            # oldest-first means the one that was refused is the one that came
            # first, so nothing behind it has a better claim.
            continue

        try:
            await repo_query(
                "UPDATE $id SET status = $status, held_reason = NONE",
                {"id": ensure_record_id(str(row.get("id"))), "status": NEW},
            )
        except Exception as exc:  # noqa: BLE001 - one job never sinks the pass
            logger.warning(f"could not promote {row.get('id')}: {exc}")
            outcome.error = f"promotion failed for {row.get('id')}: {exc}"
            continue

        outcome.promoted.append(str(row.get("id")))
        in_flight += 1
        # Class counts move with the queue, or a second job of the same class
        # would be admitted in the same pass past a limit just reached.
        for role in roles:
            counts[role] = counts.get(role, 0) + 1
        committed_cpu += sum(threads.get(r, 0) for r in roles)
        # Recompute after every promotion. Reusing a snapshot taken once per
        # pass is how a pass admits three jobs that only fit one at a time.
        committed = committed.merge(
            FootprintEstimate(models=dict(_footprint_of(row, generation_bytes).models))
        )

    outcome.in_flight_after = in_flight
    outcome.projected_bytes = committed.total
    if outcome.promoted:
        logger.info(
            f"promoted {len(outcome.promoted)} job(s); "
            f"in flight {outcome.in_flight_before}->{in_flight}; "
            f"resident {committed.total / (1024 * 1024):.0f} MB"
        )
    return outcome
