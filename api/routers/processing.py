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
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.database.repository import ensure_record_id, repo_query

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
    enabled: bool = Field(..., description="What was set")
    worker_max_tasks: int = Field(..., description="Effective value after caps")
    restart_required: bool = Field(
        default=True,
        description="Worker concurrency applies at process start, never hot",
    )
    capped: bool = Field(
        default=False,
        description="True when a platform cap lowered the requested value",
    )


#: The only key this endpoint manages, here and in every launcher that
#: reads the file. Anything else in the file is left byte-identical.
THROTTLE_KEY = "OPEN_NOTEBOOK_WORKER_MAX_TASKS"
#: Workers when full throttle is on. Not "unlimited": each worker holds a
#: model in memory, and on the 8 GB phone that is the difference between
#: fast and OOM-killed mid-transformation.
THROTTLE_FULL_TASKS = 3


def _throttle_file() -> Path:
    """Where this device keeps its throttle preference.

    Overridable for tests. The container writes into its mounted data
    dir (the repo .env is on the host, unreachable from in here);
    everywhere else it is the repository .env beside the code.
    """
    override = os.getenv("THROTTLE_FILE")
    if override:
        return Path(override)
    if Path("/.dockerenv").exists():
        return Path("/app/data/throttle.env")
    return Path(__file__).resolve().parents[2] / ".env"


def _read_throttle_key(path: Path) -> Optional[int]:
    """The `KEY=N` in `path`, or None if there is no acceptable one.

    Only a bare `KEY=N` with 1-8 is honoured; anything else in the file
    (a comment, a quoted value, a hand-edit typo, a shell fragment) is
    ignored rather than obeyed. The launchers grep for the same
    pattern, so a file this function rejects is a file they ignore
    too - the reader is deliberately the narrowest of the set.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        # No strip(), deliberately. The launchers grep
        # '^KEY=[1-8]$', anchored at both ends: a reader that tolerated
        # leading or trailing space would accept a line they ignore, and
        # the preference would work in the UI and vanish at startup.
        match = re.fullmatch(rf"{THROTTLE_KEY}=([1-8])", line)
        if match:
            found = int(match.group(1))
    return found


def _read_throttle_tasks() -> Optional[int]:
    """The persisted *preference*, or None when never set."""
    return _read_throttle_key(_throttle_file())


def _read_running_tasks() -> Optional[int]:
    """What the worker process was actually started with, or None.

    Written by the launcher at worker start. It has to be read from a
    file rather than the environment because the launcher's whole
    purpose is to override the environment: an API that reported
    `os.environ` here would name the pre-override number, so a worker
    already running at 3 would be described as running at 1, with a
    restart still pending that had in fact already happened.
    """
    return _read_throttle_key(_throttle_file().with_name("throttle.running"))


@router.post("/processing/throttle", response_model=ThrottleResponse)
async def set_throttle(body: ThrottleRequest):
    """Persist full throttle mode. Takes effect on restart, never hot.

    Worker concurrency is a process-startup setting on every launcher
    here (supervisord, termux-stack): no endpoint can safely change a
    running worker pool's size, so this writes the preference where the
    launchers read it and says so in `restart_required`. Strict is the
    default and stays it; the phone launcher additionally caps at 2 no
    matter what is written, for thermal and OOM reasons documented there.

    The request is a boolean, not a number, so there is no out-of-range
    case to refuse: what gets persisted is always 1 or
    THROTTLE_FULL_TASKS, both inside the 1-8 window the readers accept.
    """
    tasks = THROTTLE_FULL_TASKS if body.enabled else 1
    path = _throttle_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        kept = [
            line for line in lines if not line.strip().startswith(f"{THROTTLE_KEY}=")
        ]
        kept.append(f"{THROTTLE_KEY}={tasks}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot persist throttle: {exc}")

    return ThrottleResponse(
        enabled=body.enabled,
        worker_max_tasks=tasks,
        restart_required=True,
        capped=False,
    )


def _worker_max_tasks() -> int:
    """Concurrency the running worker was started with.

    The worker's own record first, the environment only as a fallback
    for a launcher that has not written one yet (a fresh container
    before the worker starts, or a bare `uvicorn` with no worker). This
    reports what is actually true, which is the only number a caller
    can act on.
    """
    recorded = _read_running_tasks()
    if recorded is not None:
        return recorded
    try:
        return max(1, int(os.getenv("OPEN_NOTEBOOK_WORKER_MAX_TASKS", "1")))
    except ValueError:
        return 1


@router.get("/processing/status")
async def processing_status() -> Dict[str, Any]:
    """Queue depth, worker concurrency, and files waiting on their owner."""
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

    running = _worker_max_tasks()
    persisted = _read_throttle_tasks()
    target = persisted if persisted is not None else running
    return {
        "worker_max_tasks": running,
        "full_throttle": running > 1,
        # True when the saved preference disagrees with the live worker:
        # the click landed, the restart is what applies it. Reported
        # rather than papered over, because a toggle that silently
        # does nothing reads as a broken app.
        "throttle_restart_pending": target != running,
        # What the next restart will actually use. The toggle must key
        # off this, not off the running value: with a change pending,
        # `full_throttle` is still the old mode and a second click would
        # write the identical preference and look like a dead button.
        "throttle_target": target,
        "queue": queue,
        "needs_retry": needs_retry,
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
    from surreal_commands import submit_command

    from open_notebook.domain.transformation import Transformation

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
            "AND status IN ['new', 'running']"
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
            submit_command(
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
