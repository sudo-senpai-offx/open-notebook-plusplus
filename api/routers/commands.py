from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field
from surreal_commands import registry

from api.command_service import CommandService
from open_notebook.ai.models import JobCheckpoint
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.exceptions import OpenNotebookError

router = APIRouter()


class CommandExecutionRequest(BaseModel):
    command: str = Field(
        ..., description="Command function name (e.g., 'generate_podcast')"
    )
    app: str = Field(..., description="Application name (e.g., 'open_notebook')")
    input: Dict[str, Any] = Field(..., description="Arguments to pass to the command")


class CommandJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class CommandJobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created: Optional[str] = None
    updated: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None


@router.post("/commands/jobs", response_model=CommandJobResponse)
async def execute_command(request: CommandExecutionRequest):
    """
    Submit a command for background processing.
    Returns immediately with job ID for status tracking.

    Example request:
    {
        "command": "generate_podcast",
        "app": "open_notebook",
        "input": {
            "episode_profile": "tech_experts",
            "speaker_profile": "tech_experts",
            "episode_name": "My Episode",
            "content": "Content to discuss"
        }
    }
    """
    try:
        # Submit command using app name (not module name)
        job_id = await CommandService.submit_command_job(
            module_name=request.app,  # This should be "open_notebook"
            command_name=request.command,
            command_args=request.input,
        )

        return CommandJobResponse(
            job_id=job_id,
            status="submitted",
            message=f"Command '{request.command}' submitted successfully",
        )

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error submitting command: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to submit command")


@router.get("/commands/jobs/{job_id}", response_model=CommandJobStatusResponse)
async def get_command_job_status(job_id: str):
    """Get the status of a specific command job"""
    try:
        status_data = await CommandService.get_command_status(job_id)
        return CommandJobStatusResponse(**status_data)

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error fetching job status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch job status")


@router.get("/commands/jobs", response_model=List[Dict[str, Any]])
async def list_command_jobs(
    command_filter: Optional[str] = Query(None, description="Filter by command name"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, description="Maximum number of jobs to return"),
):
    """List command jobs with optional filtering"""
    try:
        jobs = await CommandService.list_command_jobs(
            command_filter=command_filter, status_filter=status_filter, limit=limit
        )
        return jobs

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error listing command jobs: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list command jobs")


@router.delete("/commands/jobs/{job_id}")
async def cancel_command_job(job_id: str):
    """Cancel a running command job"""
    try:
        success = await CommandService.cancel_command_job(job_id)
        return {"job_id": job_id, "cancelled": success}

    except HTTPException:
        raise
    except OpenNotebookError:
        raise
    except Exception as e:
        logger.error(f"Error cancelling command job: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to cancel command job")


@router.get("/commands/registry/debug")
async def debug_registry():
    """Debug endpoint to see what commands are registered"""
    try:
        # Get all registered commands
        all_items = registry.get_all_commands()

        # Create JSON-serializable data
        command_items = []
        for item in all_items:
            try:
                command_items.append(
                    {
                        "app_id": item.app_id,
                        "name": item.name,
                        "full_id": f"{item.app_id}.{item.name}",
                    }
                )
            except Exception as item_error:
                logger.error(f"Error processing item: {item_error}")

        # Get the basic command structure
        try:
            commands_dict: dict[str, list[str]] = {}
            for item in all_items:
                if item.app_id not in commands_dict:
                    commands_dict[item.app_id] = []
                commands_dict[item.app_id].append(item.name)
        except Exception:
            commands_dict = {}

        return {
            "total_commands": len(all_items),
            "commands_by_app": commands_dict,
            "command_items": command_items,
        }

    except Exception as e:
        logger.error(f"Error debugging registry: {str(e)}")
        return {
            "error": str(e),
            "total_commands": 0,
            "commands_by_app": {},
            "command_items": [],
        }


# --------------------------------------------------------------------------
# checkpoint management (durable resume-on-reopen)
# --------------------------------------------------------------------------


class CheckpointSaveRequest(BaseModel):
    checkpoint: Dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque JSON progress blob interpreted by the command itself",
    )


class CheckpointResponse(BaseModel):
    command_id: str
    checkpoint: Dict[str, Any]
    attempt_count: int
    max_attempts: int
    can_retry: bool
    last_attempt_at: Optional[str] = None


class ResumeRequest(BaseModel):
    force: bool = Field(
        default=False,
        description="Force resume even if attempts are exhausted (resets counter)",
    )


class ResumeResponse(BaseModel):
    command_id: str
    resumed: bool
    status: str
    message: str


@router.post("/commands/jobs/{job_id}/checkpoint", response_model=CheckpointResponse)
async def save_checkpoint(job_id: str, body: CheckpointSaveRequest):
    """Save a checkpoint for a command job.

    Called by long-running commands to record progress so that, if the
    device restarts mid-execution, the job can resume from the last
    checkpoint rather than starting over.
    """
    try:
        checkpoint = await JobCheckpoint.upsert_for_command(
            job_id, checkpoint=body.checkpoint, increment_attempt=False
        )
        return CheckpointResponse(
            command_id=job_id,
            checkpoint=checkpoint.checkpoint,
            attempt_count=checkpoint.attempt_count,
            max_attempts=checkpoint.max_attempts,
            can_retry=await checkpoint.can_retry(),
            last_attempt_at=checkpoint.last_attempt_at,
        )
    except Exception as e:
        logger.error(f"Error saving checkpoint for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save checkpoint")


@router.get("/commands/jobs/{job_id}/checkpoint", response_model=CheckpointResponse)
async def get_checkpoint(job_id: str):
    """Get the current checkpoint for a command job."""
    try:
        checkpoint = await JobCheckpoint.get_for_command(job_id)
        if checkpoint is None:
            return CheckpointResponse(
                command_id=job_id,
                checkpoint={},
                attempt_count=0,
                max_attempts=3,
                can_retry=True,
            )
        return CheckpointResponse(
            command_id=job_id,
            checkpoint=checkpoint.checkpoint,
            attempt_count=checkpoint.attempt_count,
            max_attempts=checkpoint.max_attempts,
            can_retry=await checkpoint.can_retry(),
            last_attempt_at=checkpoint.last_attempt_at,
        )
    except Exception as e:
        logger.error(f"Error getting checkpoint for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get checkpoint")


@router.post("/commands/jobs/{job_id}/resume", response_model=ResumeResponse)
async def resume_job(job_id: str, body: ResumeRequest):
    """Manually resume a failed or interrupted command job.

    Resets the job to `held` so the dispatcher promotes it on the next
    pass. If attempts are exhausted and force is False, returns an error.
    """
    try:
        # Verify the job exists
        jid = job_id if ":" in job_id else f"command:{job_id}"
        rows = await repo_query("SELECT status FROM $id", {"id": ensure_record_id(jid)})
        if not rows:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        current_status = rows[0].get("status")

        checkpoint = await JobCheckpoint.get_for_command(job_id)
        attempts = checkpoint.attempt_count if checkpoint else 0

        if not body.force and checkpoint and not await checkpoint.can_retry():
            return ResumeResponse(
                command_id=job_id,
                resumed=False,
                status=current_status,
                message=(
                    f"Exhausted {attempts}/3 resume attempts. "
                    "Job cannot be resumed without force=true."
                ),
            )

        # Re-queue as held
        await repo_query(
            "UPDATE $id SET status = $status, error_message = NONE",
            {
                "id": ensure_record_id(jid),
                "status": "held",
            },
        )

        # Reset attempt counter if forcing
        if body.force and checkpoint:
            await JobCheckpoint.upsert_for_command(
                job_id,
                increment_attempt=False,
                checkpoint=checkpoint.checkpoint,
            )
            # Direct update to reset counter
            await repo_query(
                "UPDATE $id SET attempt_count = 0, last_attempt_at = NONE",
                {
                    "id": ensure_record_id(f"job_checkpoint:{job_id}"),
                },
            )
        elif checkpoint:
            await JobCheckpoint.upsert_for_command(
                job_id,
                increment_attempt=True,
                checkpoint=checkpoint.checkpoint,
            )

        return ResumeResponse(
            command_id=job_id,
            resumed=True,
            status="held",
            message=f"Job re-queued (attempt {attempts + 1}/3)",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resuming job {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to resume job")
