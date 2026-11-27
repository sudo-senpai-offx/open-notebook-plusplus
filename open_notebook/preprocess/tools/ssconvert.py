"""ssconvert wrapper for Week 4 poly-pipeline.

Week 4 Day 4: Spreadsheet → CSV → Markdown table conversion (Instant Lane).

Converts spreadsheet files (.xlsx, .xls, .ods) to Markdown tables via
Gnumeric's ssconvert. .csv files bypass ssconvert entirely and are read
as native UTF-8 text.

Execution: `ssconvert --export-type=Gnumeric_stf:stf_csv <input> <output.csv>`
with a 30s timeout, then formats rows into clean Markdown tables. Large
sheets are capped with a truncation warning.

Error handling: Missing binary (FileNotFoundError) or non-zero exit raises
ValueError("ssconvert_failed: ...") so extract_stage triggers fallback /
compensation.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

# SSCONVERT_BINARY resolved at call time so tests can monkeypatch it.
SSCONVERT_BINARY = "ssconvert"

SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".ods"}
CSV_EXTENSION = ".csv"

RANGE_TIMEOUT_SECONDS = 30
STAGE_TIMEOUT_SECONDS = 300

# Markdown table row cap — sheets larger than this are truncated with a
# warning rather than producing unbounded output.
MAX_TABLE_ROWS = 500


@dataclass
class ExtractResult:
    """Result of spreadsheet conversion."""

    text: str = ""
    pages_processed: int = 0
    pages_total: int = 1
    timed_out_pages: List[int] = field(default_factory=list)
    checkpoint_position: int = 0
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages_processed": self.pages_processed,
            "pages_total": self.pages_total,
            "timed_out_pages": self.timed_out_pages,
            "checkpoint_position": self.checkpoint_position,
            "truncated": self.truncated,
        }


def is_spreadsheet(file_path: Optional[str]) -> bool:
    """Whether *file_path* is a spreadsheet ssconvert can handle."""
    if not file_path:
        return False
    return os.path.splitext(file_path)[1].lower() in SPREADSHEET_EXTENSIONS


def is_csv(file_path: Optional[str]) -> bool:
    """Whether *file_path* is a CSV that bypasses ssconvert."""
    if not file_path:
        return False
    return os.path.splitext(file_path)[1].lower() == CSV_EXTENSION


def csv_to_markdown_table(csv_text: str) -> tuple[str, bool]:
    """Convert CSV text to a Markdown table.

    Returns (markdown_table, truncated). The first row is treated as the
    header. Rows beyond MAX_TABLE_ROWS are dropped and truncated=True.
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows: List[List[str]] = []
    for row in reader:
        rows.append(row)

    if not rows:
        return "", False

    truncated = len(rows) > MAX_TABLE_ROWS
    if truncated:
        rows = rows[:MAX_TABLE_ROWS]

    # Normalize column count to the widest row.
    max_cols = max(len(r) for r in rows)
    normalized = [r + [""] * (max_cols - len(r)) for r in rows]

    lines: List[str] = []
    for i, row in enumerate(normalized):
        cells = [c.replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * max_cols) + " |")

    return "\n".join(lines), truncated


async def convert_spreadsheet(
    command_id: Optional[str],
    file_path: str,
) -> Dict[str, Any]:
    """Convert a spreadsheet file to Markdown via ssconvert.

    Args:
        command_id: The command id for logging.
        file_path: Path to the spreadsheet file.

    Returns:
        ExtractResult dict with text, pages_processed, pages_total,
        timed_out_pages, checkpoint_position, and truncated.

    Raises:
        ValueError: if ssconvert is missing or returns non-zero exit.
        asyncio.TimeoutError: if conversion exceeds the timeout.
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="", pages_processed=0, pages_total=1, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    if not is_spreadsheet(file_path):
        return ExtractResult(
            text="", pages_processed=0, pages_total=1, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    # Write output CSV to a temp file.
    output_fd, output_path = tempfile.mkstemp(suffix=".csv")
    os.close(output_fd)

    try:
        cmd: List[str] = [
            SSCONVERT_BINARY,
            "--export-type=Gnumeric_stf:stf_csv",
            file_path,
            output_path,
        ]

        logger.debug(f"Running ssconvert: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=RANGE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise

            if proc.returncode != 0:
                stderr_text = stderr.decode(errors="replace")
                logger.warning(
                    f"ssconvert returned {proc.returncode} for {file_path}: {stderr_text}"
                )
                raise ValueError(
                    f"ssconvert_failed: exit code {proc.returncode}: {stderr_text.strip()}"
                )

            # Read the CSV output and convert to Markdown.
            with open(output_path, "r", encoding="utf-8", errors="replace") as f:
                csv_text = f.read()

            markdown, truncated = csv_to_markdown_table(csv_text)

            return ExtractResult(
                text=markdown,
                pages_processed=1,
                pages_total=1,
                timed_out_pages=[],
                checkpoint_position=1,
                truncated=truncated,
            ).to_dict()

        except FileNotFoundError:
            logger.error(f"ssconvert binary '{SSCONVERT_BINARY}' not found")
            raise ValueError(
                f"ssconvert_failed: binary '{SSCONVERT_BINARY}' not installed"
            )
        except asyncio.TimeoutError:
            logger.warning(f"ssconvert timed out after {RANGE_TIMEOUT_SECONDS}s for {file_path}")
            raise

    finally:
        try:
            if os.path.exists(output_path):
                os.unlink(output_path)
        except OSError:
            pass


async def read_csv_native(
    command_id: Optional[str],
    file_path: str,
) -> Dict[str, Any]:
    """Read a CSV file as native UTF-8 text (bypasses ssconvert).

    Args:
        command_id: The command id for logging.
        file_path: Path to the CSV file.

    Returns:
        ExtractResult dict with the file's text content.
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="", pages_processed=0, pages_total=1, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        logger.warning(f"Failed to read CSV {file_path}: {exc}")
        return ExtractResult(
            text="", pages_processed=0, pages_total=1, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    return ExtractResult(
        text=text,
        pages_processed=1,
        pages_total=1,
        timed_out_pages=[],
        checkpoint_position=1,
    ).to_dict()
