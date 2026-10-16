"""Device-local processing control: throttle mode, scheduling, retries.

What lives here
---------------
Three endpoints the sync channel deliberately does not cover, because
they act on this device's queue rather than exchanging state:

* `GET /processing/status` - queue depth by status, the active worker
  concurrency, and which sources wait on their owner. Read-only and
  cheap; the UI polls it on mount and on manual refresh, never on a
  timer, so an idle phone pays nothing.
* `POST /processing/ensure-insights` - queue every missing
  (source, transformation) pair, smallest files first, skipping pairs
  that already have an insight and sources with no text (a
  transformation with no input invents output). Idempotent: running it
  twice queues nothing the second time.
* `POST /processing/retry-failed` - clear the command refs of failed or
  canceled extraction jobs so the normal scan re-queues them. Explicit
  and auditable, never automatic: a failure deserves eyes first.

What does NOT live here
-----------------------
Cross-device execution. A shared queue would need a live coordination
channel between databases that are separate by design (offline-first),
per-job byte transfers to the executing device, and result merging
through a path narrower than the manifest already is - while the phone
is slower per job than the PC, thermally limited, and battery-bound,
and the two devices run different models (so device-flavored content
would land in one shared library). The distributed design already
exists: generate once on the fastest device, sync the results
everywhere. These endpoints make the local half of that fast and
visible instead.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.scheduler.dispatcher import (
    DEFAULT_INTERVAL_SECONDS as READ_INTERVAL,
)
from open_notebook.scheduler.dispatcher import (
    device_cap,
    read_window,
    write_window,
)
from open_notebook.scheduler.gate import default_reserve
from open_notebook.scheduler.memory import current_memory

#: This device's hard ceiling on the window. The phone launcher lowers it; an
#: 8 GB device with two model servers beside the worker will OOM past 2.
THROTTLE_CAP = device_cap()
THROTTLE_FULL_TASKS = 3

router = APIRouter()


class EnsureInsightsRequest(BaseModel):
    transformation_ids: Optional[List[str]] = Field(
        default=None, description="Transformations to run; default all"
    )
    source_ids: Optional[List[str]] = Field(
        default=None, description="Sources to cover; default all"
    )
    limit: int = Field(default=0, description="Cap pairs queued; 0 means all")


class EnsureInsightsResponse(BaseModel):
    queued: int = Field(..., description="Pairs submitted")
    skipped_have_insight: int = Field(..., description="Already generated")
    skipped_no_text: int = Field(..., description="Nothing to transform")
    skipped_unknown: int = Field(..., description="Missing source or transformation")


class RetryFailedRequest(BaseModel):
    source_ids: Optional[List[str]] = Field(
        default=None, description="Sources to retry; default all failed"
    )


class RetryFailedResponse(BaseModel):
    cleared: List[str] = Field(..., description="Source ids released for re-queue")
    cleared_count: int = Field(..., description="How many")


class ThrottleRequest(BaseModel):
    enabled: bool = Field(..., description="True for full throttle, false for strict")


class ThrottleResponse(BaseModel):
    enabled: bool = Field(..., description="True when the window is above strict")
    #: The promotion window. Not a worker semaphore: the dispatcher admits at
    #: most this many jobs, and the vendor worker's own limit is a backstop
    #: that should never bind.
    window: int = Field(..., description="Effective window after caps")
    #: Always False. The window is read fresh on every dispatcher pass, so a
    #: toggle applies on the next pass rather than at a restart.
    restart_required: bool = Field(
        default=False, description="Always False: the window is read live"
    )
    capped: bool = Field(
        default=False, description="True when a platform cap lowered the request"
    )
    applied_within_seconds: int = Field(
        default=0, description="Upper bound on when the new window takes effect"
    )


@router.post("/processing/throttle", response_model=ThrottleResponse)
async def set_throttle(body: ThrottleRequest):
    """Set the promotion window. Applies on the next dispatcher pass.

    Supersedes the restart-based throttle from `beef791`. That design treated
    worker concurrency as a process-startup setting, because
    `surreal_commands` builds its semaphore once before the listen loop. It
    therefore persisted a preference, reported `restart_required`, and had
    both launchers record what they had actually started with so the API
    could reconcile the file against the running process.

    Concurrency now lives *upstream* of the claim. The dispatcher decides how
    many jobs are ever `new`, so the value is read fresh each pass and there
    is nothing to reconcile, nothing to restart, and no window in which the UI
    could truthfully claim a change had not yet taken hold.

    Shrinking applies going forward. Jobs already running are left alone:
    killing one strands in-flight work as a stale failure, which costs more
    than the memory it would reclaim.
    """
    tasks = THROTTLE_FULL_TASKS if body.enabled else 1
    capped = tasks > THROTTLE_CAP
    try:
        # write_window applies the cap itself, so the stored value and the
        # reported value cannot disagree. A device ceiling that only clamped
        # on read would leave the file saying 3 and the window at 2, which
        # is the kind of discrepancy that reads as a broken toggle.
        written = write_window(tasks)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot persist throttle: {exc}")

    return ThrottleResponse(
        enabled=written > 1,
        window=written,
        restart_required=False,
        capped=capped,
        applied_within_seconds=int(READ_INTERVAL),
    )


@router.get("/processing/status")
async def processing_status() -> Dict[str, Any]:
    """Queue depth, the live window, memory headroom, and why work is held.

    Everything reported here is something the dispatcher actually knows. The
    superseded design reported a *preference* alongside a *running value* and a
    pending flag, because the two could disagree for an unbounded time. There
    is no such disagreement to report now: the window is read fresh each pass.
    """
    try:
        states = (
            await repo_query("SELECT status, count() AS n FROM command GROUP BY status")
            or []
        )
        queue = {str(r.get("status")): r.get("n") for r in states}
    except Exception as exc:  # noqa: BLE001 - status never breaks callers
        logger.warning(f"processing status unavailable: {exc}")
        queue = {}

    needs_retry: List[str] = []
    try:
        rows = (
            await repo_query(
                "SELECT id, command FROM source "
                "WHERE command != NONE AND (full_text = NONE OR full_text = '')"
            )
            or []
        )
        from open_notebook.sync.extraction import _skip_reason

        for row in rows:
            if await _skip_reason(str(row.get("command"))) == "needs a look":
                needs_retry.append(str(row.get("id")))
    except Exception as exc:  # noqa: BLE001 - best effort enrichment
        logger.debug(f"needs_retry unavailable: {exc}")

    # Why the held jobs are held, grouped. A queue that is not draining is
    # only diagnosable if "nothing is running" and "everything is waiting on
    # memory" are distinguishable, and the UI needs the counts to say which.
    held_reasons: Dict[str, int] = {}
    try:
        rows = (
            await repo_query(
                "SELECT held_reason, count() AS n FROM command "
                "WHERE status = 'held' GROUP BY held_reason"
            )
            or []
        )
        for row in rows:
            reason = row.get("held_reason") or "not-yet-considered"
            held_reasons[str(reason)] = int(row.get("n") or 0)
    except Exception as exc:  # noqa: BLE001 - best effort enrichment
        logger.debug(f"held_reasons unavailable: {exc}")

    memory = current_memory()
    window = read_window()
    in_flight = int(queue.get("new", 0) or 0) + int(queue.get("running", 0) or 0)

    return {
        # The promotion window, read live. `full_throttle` is derived from it
        # rather than from a separate flag so the two can never disagree.
        "window": window,
        "full_throttle": window > 1,
        "in_flight": in_flight,
        "queue": queue,
        "held_reasons": held_reasons,
        "needs_retry": needs_retry,
        # Memory, in bytes, with `readable` rather than a zero standing in
        # for "unknown" - a zero here reads as "this device has no memory",
        # which is a different and much more alarming statement.
        "memory": {
            "readable": memory.readable,
            "total": memory.total,
            "available": memory.available,
            "swapped": memory.swapped,
            "reserve": default_reserve(memory.total) if memory.readable else 0,
        },
    }


@router.post("/processing/ensure-insights", response_model=EnsureInsightsResponse)
async def ensure_insights(body: EnsureInsightsRequest):
    """Queue every missing (source, transformation) pair, smallest first.

    Dedupes against existing insights by (source, transformation title),
    so re-running after an interruption - or after the next deploy -
    only fills gaps. Sources without text are skipped, not queued: the
    endpoint refuses to pay for invented content, same as the single
    insight endpoint. Smallest files go first so results start landing
    immediately and problem files surface early instead of last.
    """
    from open_notebook.domain.transformation import Transformation
    from open_notebook.scheduler.submit import submit_held

    wanted_transformations = body.transformation_ids
    wanted_sources = body.source_ids

    transformations = await repo_query("SELECT id, title FROM transformation") or []
    if wanted_transformations:
        wanted = set(wanted_transformations)
        transformations = [t for t in transformations if str(t.get("id")) in wanted]

    sources = await repo_query("SELECT id, title, asset FROM source") or []
    if wanted_sources:
        wanted = set(wanted_sources)
        sources = [s for s in sources if str(s.get("id")) in wanted]

    have = await repo_query("SELECT source, insight_type FROM source_insight") or []
    done = {(str(r.get("source")), r.get("insight_type")) for r in have}

    # Pairs already queued must not be queued again: two jobs for one
    # pair both complete (neither sees the other coming) and the source
    # ends up with twin insights. The queue is part of the dedup key,
    # not just the table.
    title_by_id = {str(t.get("id")): t.get("title") for t in transformations}
    pending_rows = (
        await repo_query(
            "SELECT args FROM command WHERE name = 'run_transformation' "
            "AND status IN ['held', 'new', 'running']"
        )
        or []
    )
    for row in pending_rows:
        try:
            args = row.get("args")
            if isinstance(args, str):
                args = json.loads(args)
            pending_sid = str((args or {}).get("source_id", ""))
            pending_title = title_by_id.get(
                str((args or {}).get("transformation_id", ""))
            )
            if pending_sid and pending_title:
                done.add((pending_sid, pending_title))
        except Exception:  # noqa: BLE001 - one unreadable row skips nothing else
            continue

    def _size(source: Dict[str, Any]) -> int:
        asset = source.get("asset") or {}
        try:
            return int(asset.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    queued = skipped_have = skipped_text = skipped_unknown = 0
    pairs: List[tuple] = []
    for source in sources:
        sid = str(source.get("id"))
        for transformation in transformations:
            tid = str(transformation.get("id"))
            pairs.append((_size(source), sid, tid, transformation.get("title")))

    # Smallest first: early results, early failures, steady progress
    # visible instead of one long silence.
    pairs.sort(key=lambda p: p[0])

    for _size_value, sid, tid, title in pairs:
        if body.limit and queued >= body.limit:
            break
        try:
            full_source = await repo_query(
                "SELECT full_text FROM source WHERE id = $id",
                {"id": ensure_record_id(sid)},
            )
        except Exception:
            skipped_unknown += 1
            continue
        if not full_source:
            skipped_unknown += 1
            continue
        text = (full_source[0].get("full_text") or "").strip()
        if not text:
            skipped_text += 1
            continue
        if (sid, title) in done:
            skipped_have += 1
            continue
        try:
            await Transformation.get(tid)
        except Exception:
            skipped_unknown += 1
            continue
        try:
            submit_held(
                "open_notebook",
                "run_transformation",
                {"source_id": sid, "transformation_id": tid},
            )
            done.add((sid, title))
            queued += 1
        except Exception as exc:  # noqa: BLE001 - one pair never sinks the batch
            logger.warning(f"could not queue {tid} for {sid}: {exc}")
            skipped_unknown += 1

    return EnsureInsightsResponse(
        queued=queued,
        skipped_have_insight=skipped_have,
        skipped_no_text=skipped_text,
        skipped_unknown=skipped_unknown,
    )


@router.post("/processing/retry-failed", response_model=RetryFailedResponse)
async def retry_failed(body: RetryFailedRequest):
    """Release failed/canceled extraction jobs for re-queueing.

    Clears the command reference so the normal scan picks the source up
    again; it never touches live jobs or sources that already have text.
    Explicit by design: this is the human decision the automatic scan
    refuses to make, now behind one auditable call instead of a raw
    database write.
    """
    from open_notebook.sync.extraction import _skip_reason

    wanted = set(body.source_ids or [])
    rows = await repo_query("SELECT id, command FROM source") or []
    cleared: List[str] = []
    for row in rows:
        sid = str(row.get("id"))
        if wanted and sid not in wanted:
            continue
        ref = row.get("command")
        if not ref:
            continue
        try:
            if await _skip_reason(str(ref)) == "needs a look":
                await repo_query(
                    "UPDATE $id SET command = NONE",
                    {"id": ensure_record_id(sid)},
                )
                cleared.append(sid)
        except Exception as exc:  # noqa: BLE001 - per-record, never fatal
            logger.warning(f"could not release {sid}: {exc}")

    return RetryFailedResponse(cleared=cleared, cleared_count=len(cleared))
