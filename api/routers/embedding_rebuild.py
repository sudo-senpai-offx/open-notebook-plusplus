from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from surreal_commands import get_command_status

from api.command_service import CommandService
from api.models import (
    RebuildProgress,
    RebuildRequest,
    RebuildResponse,
    RebuildStats,
    RebuildStatusResponse,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import OpenNotebookError

router = APIRouter()

#: Job states this API understands, and how they map onto the four the
#: response model documents. `held` is the important one: it is the state the
#: dispatcher parks work in, and it means "waiting", which is `queued`.
_PUBLIC_STATUS = {
    "held": "queued",
    "new": "queued",
    "queued": "queued",
    "pending": "queued",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "canceled": "failed",
}


def _public_status(raw: str) -> str:
    """A stored status, in the vocabulary this API promises.

    An unrecognised value is passed through rather than folded into
    `completed`: reporting a running rebuild as finished would be a lie in
    the direction that matters.
    """
    return _PUBLIC_STATUS.get(raw, raw or "queued")


async def _raw_status(command_id: str) -> Optional[str]:
    """The status column, read without the vendor's enum in the way.

    `ensure_record_id` because a plain string matches no record id in
    SurrealDB - the query would return nothing at all and look like a
    missing command rather than an enum gap.
    """
    try:
        rows = await repo_query(
            "SELECT status FROM $id", {"id": ensure_record_id(command_id)}
        )
    except Exception as exc:  # noqa: BLE001 - status never breaks callers
        logger.warning(f"could not read raw status for {command_id}: {exc}")
        return None
    for row in rows or []:
        value = row.get("status")
        if value:
            return str(value)
    return None


@router.post("/rebuild", response_model=RebuildResponse)
async def start_rebuild(request: RebuildRequest):
    """
    Start a background job to rebuild embeddings.

    - **mode**: "existing" (re-embed items with embeddings) or "all" (embed everything)
    - **include_sources**: Include sources in rebuild (default: true)
    - **include_notes**: Include notes in rebuild (default: true)
    - **include_insights**: Include insights in rebuild (default: true)

    Returns command ID to track progress and estimated item count.
    """
    try:
        logger.info(f"Starting rebuild request: mode={request.mode}")

        # Import commands to ensure they're registered
        import commands.embedding_commands  # noqa: F401

        # Estimate total items (quick count query)
        # This is a rough estimate before the command runs
        total_estimate = 0

        if request.include_sources:
            if request.mode == "existing":
                # Count sources with embeddings
                result = await repo_query(
                    """
                    SELECT VALUE count(array::distinct(
                        SELECT VALUE source.id
                        FROM source_embedding
                        WHERE embedding != none AND array::len(embedding) > 0
                    )) as count FROM {}
                    """
                )
            else:
                # Count all sources with content
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source WHERE full_text != none GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        if request.include_notes:
            if request.mode == "existing":
                result = await repo_query(
                    "SELECT VALUE count() as count FROM note WHERE embedding != none AND array::len(embedding) > 0 GROUP ALL"
                )
            else:
                result = await repo_query(
                    "SELECT VALUE count() as count FROM note WHERE content != none GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        if request.include_insights:
            if request.mode == "existing":
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source_insight WHERE embedding != none AND array::len(embedding) > 0 GROUP ALL"
                )
            else:
                result = await repo_query(
                    "SELECT VALUE count() as count FROM source_insight GROUP ALL"
                )

            if result and isinstance(result[0], dict):
                total_estimate += result[0].get("count", 0)
            elif result:
                total_estimate += result[0] if isinstance(result[0], int) else 0

        logger.info(f"Estimated {total_estimate} items to process")

        # Submit command
        command_id = await CommandService.submit_command_job(
            "open_notebook",
            "rebuild_embeddings",
            {
                "mode": request.mode,
                "include_sources": request.include_sources,
                "include_notes": request.include_notes,
                "include_insights": request.include_insights,
            },
        )

        logger.info(f"Submitted rebuild command: {command_id}")

        return RebuildResponse(
            command_id=command_id,
            total_items=total_estimate,
            message=f"Rebuild operation started. Estimated {total_estimate} items to process.",
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Failed to start rebuild: {e}")
        logger.exception(e)
        raise HTTPException(
            status_code=500, detail=f"Failed to start rebuild operation: {str(e)}"
        )


@router.get("/rebuild/{command_id}/status", response_model=RebuildStatusResponse)
async def get_rebuild_status(command_id: str):
    """
    Get the status of a rebuild operation.

    Returns:
    - **status**: queued, running, completed, failed
    - **progress**: processed count, total count, percentage
    - **stats**: breakdown by type (sources, notes, insights, failed)
    - **timestamps**: started_at, completed_at
    """
    try:
        # Get command status from surreal_commands
        #
        # That call coerces the stored status into the vendor's
        # `CommandStatus` enum, which has no `held` member - so it raised
        # `ValueError: 'held' is not a valid CommandStatus` and this endpoint
        # answered 500. `held` is not an exotic state here: it is how every
        # job is created, and on a memory-gated device the queue is *mostly*
        # held, so this endpoint broke precisely while someone was watching a
        # rebuild drain.
        #
        # A held job is waiting its turn, which is what `queued` already means
        # to a caller. Translate the raw status instead of letting an enum gap
        # become an opaque failure.
        try:
            status = await get_command_status(command_id)
        except Exception as exc:  # noqa: BLE001 - any coercion failure lands here
            raw = await _raw_status(command_id)
            if raw is None:
                raise HTTPException(
                    status_code=404, detail="Rebuild command not found"
                ) from exc
            return RebuildStatusResponse(
                command_id=command_id, status=_public_status(raw)
            )

        if not status:
            raise HTTPException(status_code=404, detail="Rebuild command not found")

        # Build response based on status
        response = RebuildStatusResponse(
            command_id=command_id,
            status=status.status,
        )

        # Extract metadata from command result
        if status.result and isinstance(status.result, dict):
            result = status.result

            # Build progress info
            if "total_items" in result and "jobs_submitted" in result:
                total = result["total_items"]
                submitted = result["jobs_submitted"]
                response.progress = RebuildProgress(
                    processed=submitted,
                    total=total,
                    percentage=round((submitted / total * 100) if total > 0 else 0, 2),
                )

            # Build stats
            response.stats = RebuildStats(
                sources=result.get("sources_submitted", 0),
                notes=result.get("notes_submitted", 0),
                insights=result.get("insights_submitted", 0),
                failed=result.get("failed_submissions", 0),
            )

        # Add timestamps
        if hasattr(status, "created") and status.created:
            response.started_at = str(status.created)
        if hasattr(status, "updated") and status.updated:
            response.completed_at = str(status.updated)

        # Add error message if failed
        if (
            status.status == "failed"
            and status.result
            and isinstance(status.result, dict)
        ):
            response.error_message = status.result.get("error_message", "Unknown error")

        return response

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Failed to get rebuild status: {e}")
        logger.exception(e)
        raise HTTPException(
            status_code=500, detail=f"Failed to get rebuild status: {str(e)}"
        )
