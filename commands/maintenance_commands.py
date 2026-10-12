import os
import time
from typing import Dict, Optional

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.maintenance.prune import (
    PRUNE_TABLES,
    cutoff_iso,
    export_and_prune,
    retention_days,
)
from open_notebook.utils.job_control import (
    Heartbeat,
    JobCancelledError,
    get_current_command_id,
    raise_if_cancelled,
    report_progress_record,
)


class PruneEventLogsInput(CommandInput):
    dry_run: bool = False


class PruneEventLogsOutput(CommandOutput):
    success: bool
    tables: Dict[str, Dict[str, object]] = {}
    processing_time: float = 0.0
    error_message: Optional[str] = None


@command(
    "prune_event_logs",
    app="open_notebook",
    retry={
        "max_attempts": 2,
        "wait_strategy": "exponential_jitter",
        "wait_min": 1,
        "wait_max": 30,
        "stop_on": [JobCancelledError, ValueError],
        "retry_log_level": "warning",
    },
)
async def prune_event_logs_command(
    input_data: PruneEventLogsInput,
) -> PruneEventLogsOutput:
    """Export-then-prune event logs older than the retention window.

    Dry runs report counts without writing or deleting.
    """
    start_time = time.time()
    cmd_id = get_current_command_id() or (
        str(input_data.execution_context.command_id)
        if input_data.execution_context
        else None
    )
    hb = Heartbeat(cmd_id)
    hb.start()
    try:
        days = retention_days()
        cutoff = cutoff_iso(days)
        export_dir = os.getenv("OPEN_NOTEBOOK_EXPORT_DIR", "./notebook_export")
        tables: Dict[str, Dict[str, object]] = {}
        for index, table in enumerate(PRUNE_TABLES):
            await raise_if_cancelled(cmd_id)
            await report_progress_record(
                cmd_id,
                progress=10 + int(80 * index / len(PRUNE_TABLES)),
                stage=f"pruning_{table}",
            )
            if input_data.dry_run:
                tables[table] = {"dry_run": True, "cutoff": cutoff}
                continue
            tables[table] = await export_and_prune(table, cutoff, export_dir)
        await report_progress_record(cmd_id, progress=100, stage="completed")
        return PruneEventLogsOutput(
            success=True,
            tables=tables,
            processing_time=time.time() - start_time,
        )
    except Exception as e:
        logger.debug(f"Transient prune failure: {e}")
        raise
    finally:
        try:
            hb.stop()
        except Exception:
            pass
