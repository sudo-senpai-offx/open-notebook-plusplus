import time
from typing import List, Optional

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from api.command_service import CommandService
from commands.source_commands import SourceProcessingInput
from open_notebook.database.repository import ensure_record_id
from open_notebook.domain.notebook import Asset, Source
from open_notebook.utils.job_control import (
    Heartbeat,
    JobCancelledError,
    get_current_command_id,
    raise_if_cancelled,
    report_progress_record,
)
from open_notebook.vault.ingest import (
    load_watermark,
    save_watermark,
    scan_vault,
    vault_dir,
    watermark_path,
)


class VaultIngestInput(CommandInput):
    notebook_ids: List[str] = []
    embed: bool = False


class VaultIngestOutput(CommandOutput):
    success: bool
    ingested: int = 0
    processing_time: float = 0.0
    error_message: Optional[str] = None


@command(
    "ingest_vault",
    app="open_notebook",
    retry={
        "max_attempts": 3,
        "wait_strategy": "exponential_jitter",
        "wait_min": 1,
        "wait_max": 60,
        "stop_on": [JobCancelledError, ValueError],
        "retry_log_level": "warning",
    },
)
async def ingest_vault_command(input_data: VaultIngestInput) -> VaultIngestOutput:
    """One-shot vault scan: new/modified markdown becomes sources.

    Files stay where they are (vault may be read-only): content_state
    points at the vault path with delete_source=False. The watch loop that
    triggers this on a schedule is a later slice.
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
        await report_progress_record(cmd_id, progress=5, stage="scanning")
        await raise_if_cancelled(cmd_id)
        directory = vault_dir()
        wpath = watermark_path()
        watermark = load_watermark(wpath)
        found, updated = scan_vault(directory, watermark)
        logger.info(f"Vault scan {directory}: {len(found)} new file(s)")

        ingested = 0
        for path in found:
            await raise_if_cancelled(cmd_id)
            source = Source(
                title=path.stem or "Processing...",
                asset=Asset(file_path=str(path)),
            )
            await source.save()
            for notebook_id in input_data.notebook_ids:
                await source.add_to_notebook(notebook_id)
            command_id = await CommandService.submit_command_job(
                "open_notebook",
                "process_source",
                SourceProcessingInput(
                    source_id=str(source.id),
                    content_state={
                        "file_path": str(path),
                        "delete_source": False,
                    },
                    notebook_ids=input_data.notebook_ids,
                    transformations=[],
                    embed=input_data.embed,
                ).model_dump(),
            )
            source.command = ensure_record_id(command_id)
            await source.save()
            ingested += 1
            await report_progress_record(
                cmd_id,
                progress=5 + int(90 * ingested / max(1, len(found))),
                stage="submitted",
            )

        save_watermark(wpath, updated)
        await report_progress_record(cmd_id, progress=100, stage="completed")
        return VaultIngestOutput(
            success=True,
            ingested=ingested,
            processing_time=time.time() - start_time,
        )
    except Exception as e:
        logger.debug(f"Transient vault ingest failure: {e}")
        raise
    finally:
        try:
            hb.stop()
        except Exception:
            pass
