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

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
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


def _worker_max_tasks() -> int:
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
        from open_notebook.sync.extraction import (
            enqueue_missing_extraction,  # noqa: F401
        )
    except ImportError:
        pass
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

    return {
        "worker_max_tasks": _worker_max_tasks(),
        "full_throttle": _worker_max_tasks() > 1,
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
