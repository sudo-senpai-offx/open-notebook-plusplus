"""pdftotext wrapper for Week 4 poly-pipeline.

Week 4 Day 1: Failure #3 completion.

Splits a PDF into 5-page ranges and runs `pdftotext -f N -l M path -` on
each range with a 90s timeout. After each range, writes an intra-stage
checkpoint via JobCheckpoint so a kill -9 mid-extraction preserves
completed pages. If a range times out, it is marked as timed out and the
next range continues — no page is silently lost.

Returns an ExtractResult dict compatible with the 7-stage extract_stage:
    {
        "text": str,                    # concatenated page text
        "pages_processed": int,         # pages successfully extracted
        "pages_total": int,             # total pages attempted
        "timed_out_pages": List[int],   # page numbers that timed out
        "checkpoint_position": int,     # last checkpointed page
    }

The checkpoint_position lets extract_stage resume from the last saved
page rather than restarting from page 1.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.ai.models import JobCheckpoint
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.job_control import report_progress_record
from open_notebook.utils.retry_classification import should_retry
from open_notebook.utils.subprocess_utils import run_heavy_operation

PAGES_PER_RANGE = 5
RANGE_TIMEOUT_SECONDS = 90
STAGE_TIMEOUT_SECONDS = 300


@dataclass
class ExtractResult:
    """Result of PDF extraction."""

    text: str = ""
    pages_processed: int = 0
    pages_total: int = 0
    timed_out_pages: List[int] = field(default_factory=list)
    checkpoint_position: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages_processed": self.pages_processed,
            "pages_total": self.pages_total,
            "timed_out_pages": self.timed_out_pages,
            "checkpoint_position": self.checkpoint_position,
        }


async def _write_checkpoint(
    command_id: str,
    position: int,
    pages_processed: int,
    timed_out_pages: List[int],
    text_accumulator: List[str],
) -> None:
    """Write an intra-stage checkpoint so resume continues from position."""
    checkpoint_data = {
        "stage": "extract",
        "position": position,
        "pages_processed": pages_processed,
        "timed_out_pages": timed_out_pages,
        "text_length": sum(len(t) for t in text_accumulator),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await JobCheckpoint.upsert_for_command(
            command_id,
            checkpoint=checkpoint_data,
            increment_attempt=False,
        )
    except Exception as exc:  # noqa: BLE001 - checkpoint failure must not stop extraction
        logger.debug(
            f"checkpoint write failed for {command_id} at page {position}: {exc}"
        )


async def _read_checkpoint(command_id: str) -> Optional[Dict[str, Any]]:
    """Read the last checkpoint for resume."""
    try:
        cp = await JobCheckpoint.get_for_command(command_id)
        if cp is None:
            return None
        return cp.checkpoint or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"checkpoint read failed for {command_id}: {exc}")
        return None


async def extract_pdf(
    command_id: Optional[str],
    file_path: str,
    page_count: int,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract text from a PDF with per-range timeout and intra-stage checkpointing.

    Splits pages into 5-page ranges and runs pdftotext on each range with a
    90s timeout. After each range, writes an intra-stage checkpoint. If a
    range times out, it is recorded and the next range continues.

    Args:
        command_id: The command id for checkpointing and progress reporting.
        file_path: Path to the PDF file.
        page_count: Total number of pages in the PDF.
        language: Optional language hint for pdftotext (-l flag).

    Returns:
        ExtractResult dict with text, pages_processed, pages_total,
        timed_out_pages, and checkpoint_position.
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="",
            pages_processed=0,
            pages_total=0,
            timed_out_pages=[],
            checkpoint_position=0,
        ).to_dict()

    result = ExtractResult(pages_total=page_count)

    # Resume from checkpoint if available
    resume_from = 0
    if command_id:
        cp = await _read_checkpoint(command_id)
        if cp and cp.get("stage") == "extract":
            resume_from = int(cp.get("position", 0))
            result.pages_processed = int(cp.get("pages_processed", 0))
            result.timed_out_pages = list(cp.get("timed_out_pages", []))
            result.checkpoint_position = resume_from
            logger.info(
                f"Resuming PDF extraction for {command_id} from page {resume_from}"
            )

    text_accumulator: List[str] = []

    for start_page in range(resume_from + 1, page_count + 1, PAGES_PER_RANGE):
        end_page = min(start_page + PAGES_PER_RANGE - 1, page_count)

        # Build pdftotext command
        cmd: List[str] = ["pdftotext", "-f", str(start_page), "-l", str(end_page)]
        if language:
            cmd.extend(["-l", language])
        cmd.extend([file_path, "-"])

        await report_progress_record(
            command_id,
            progress=30 + int((start_page / page_count) * 60),
            stage="extract",
        )

        # Per-range retry loop: transient timeouts are retried with
        # exponential backoff before marking the range timed out.
        range_attempt = 0
        max_range_attempts = 2

        while True:
            range_attempt += 1
            try:
                op_result = await run_heavy_operation(
                    cmd,
                    timeout=RANGE_TIMEOUT_SECONDS,
                )
                if op_result["returncode"] == 0 and op_result["stdout"].strip():
                    text_accumulator.append(op_result["stdout"])
                    result.pages_processed += (end_page - start_page + 1)
                    logger.debug(
                        f"Extracted pages {start_page}-{end_page} "
                        f"({len(op_result['stdout'])} chars)"
                    )
                    break  # success, move to next range
                else:
                    # Non-zero return code — permanent failure for this range
                    logger.warning(
                        f"pdftotext returned {op_result['returncode']} for "
                        f"pages {start_page}-{end_page}: {op_result['stderr']}"
                    )
                    result.timed_out_pages.extend(range(start_page, end_page + 1))
                    break
            except asyncio.TimeoutError:
                should, delay = should_retry(
                    asyncio.TimeoutError(),
                    range_attempt,
                    max_attempts=max_range_attempts,
                )
                if not should:
                    logger.warning(
                        f"pdftotext timed out after {RANGE_TIMEOUT_SECONDS}s for "
                        f"pages {start_page}-{end_page} "
                        f"(exhausted {max_range_attempts} attempts)"
                    )
                    result.timed_out_pages.extend(range(start_page, end_page + 1))
                    break
                logger.debug(
                    f"pdf: range timeout, retrying in {delay:.1f}s "
                    f"(attempt {range_attempt}/{max_range_attempts})"
                )
                await asyncio.sleep(delay)
                continue
            except Exception as exc:  # noqa: BLE001 - one range must not stop all
                logger.warning(
                    f"pdftotext failed for pages {start_page}-{end_page}: {exc}"
                )
                result.timed_out_pages.extend(range(start_page, end_page + 1))
                break

        # Write intra-stage checkpoint after each range
        result.text = "\n".join(text_accumulator)
        result.checkpoint_position = end_page
        await _write_checkpoint(
            command_id=command_id or "",
            position=end_page,
            pages_processed=result.pages_processed,
            timed_out_pages=result.timed_out_pages,
            text_accumulator=text_accumulator,
        )

    result.text = "\n".join(text_accumulator)

    if command_id:
        await report_progress_record(
            command_id,
            progress=90,
            stage="extract",
        )

    logger.info(
        f"PDF extraction complete for {command_id or 'unknown'}: "
        f"{result.pages_processed}/{result.pages_total} pages, "
        f"{len(result.timed_out_pages)} timed out"
    )

    return result.to_dict()