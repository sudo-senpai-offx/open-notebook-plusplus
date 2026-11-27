"""pandoc wrapper for Week 4 poly-pipeline.

Week 4 Day 2: Document conversion wrapper (docx, odt, epub, rtf, html).

Converts rich documents to markdown via pandoc. Includes OOM protections:

- HTML input: strips base64 image URIs before pandoc (prevents bloated HTML
  from stalling the parser).
- DOCX input: unzips, strips images from word/media/, re-zips before pandoc
  (prevents 3 GB OOM on Android when converting image-heavy DOCX files).

Returns an ExtractResult dict compatible with extract_stage:

    {
        "text": str,                    # converted markdown/text
        "pages_processed": int,         # always 1 (single document)
        "pages_total": int,             # always 1
        "timed_out_pages": List[int],   # empty unless timeout
        "checkpoint_position": int,     # 1 after conversion
    }
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

# PANDOC_BINARY resolved at call time so tests can monkeypatch it.
PANDOC_BINARY = "pandoc"

RANGE_TIMEOUT_SECONDS = 300
STAGE_TIMEOUT_SECONDS = 300


@dataclass
class ExtractResult:
    """Result of document conversion."""

    text: str = ""
    pages_processed: int = 0
    pages_total: int = 1
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


def strip_html_base64(html: str) -> str:
    """Strip inline data:image/...;base64,... strings from HTML.

    Prevents bloated inline assets from stalling pandoc and consuming
    excessive memory on Android.
    """
    import re

    return re.sub(
        r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', "", html
    )


def strip_docx_media(docx_path: str) -> str:
    """Unzip a DOCX, strip images from word/media/, re-zip to a temp file.

    Returns the path to the stripped DOCX. This prevents pandoc from
    attempting to embed large images and OOMing on Android.
    """
    import re

    stripped_fd, stripped_path = tempfile.mkstemp(suffix=".docx")
    os.close(stripped_fd)

    with zipfile.ZipFile(docx_path, "r") as zin:
        with zipfile.ZipFile(stripped_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                # Skip image files in word/media/
                if item.filename.startswith("word/media/"):
                    logger.debug(f"Stripping DOCX media: {item.filename}")
                    continue
                zout.writestr(item, zin.read(item.filename))

    return stripped_path


async def convert_document(
    command_id: Optional[str],
    file_path: str,
    source_format: Optional[str] = None,
    language: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a document to markdown via pandoc.

    Args:
        command_id: The command id for logging.
        file_path: Path to the document file.
        source_format: Source format hint (docx, odt, epub, rtf, html, auto).
        language: Optional language hint.

    Returns:
        ExtractResult dict with text, pages_processed, pages_total,
        timed_out_pages, and checkpoint_position.

    Raises:
        FileNotFoundError: if pandoc binary is not installed.
        asyncio.TimeoutError: if conversion exceeds the timeout.
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="", pages_processed=0, pages_total=1, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    ext = os.path.splitext(file_path)[1].lower()

    # Determine pandoc input format
    if source_format and source_format != "auto":
        input_format = source_format
    elif ext == ".docx":
        input_format = "docx"
    elif ext == ".odt":
        input_format = "odt"
    elif ext == ".epub":
        input_format = "epub"
    elif ext == ".rtf":
        input_format = "rtf"
    elif ext in (".html", ".htm", ".xhtml"):
        input_format = "html"
    else:
        input_format = "auto"

    # Pre-process based on file type
    working_path = file_path
    cleanup_paths: List[str] = []

    try:
        if ext == ".html" or ext == ".htm" or ext == ".xhtml":
            # Strip base64 image URIs from HTML before pandoc
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                html_content = f.read()
            html_content = strip_html_base64(html_content)
            stripped_fd, stripped_path = tempfile.mkstemp(suffix=".html")
            with os.fdopen(stripped_fd, "w", encoding="utf-8") as f:
                f.write(html_content)
            working_path = stripped_path
            cleanup_paths.append(stripped_path)

        elif ext == ".docx":
            # Unzip, strip word/media/, re-zip to prevent OOM
            working_path = strip_docx_media(file_path)
            cleanup_paths.append(working_path)

        # Build pandoc command
        pandoc_bin = PANDOC_BINARY
        cmd: List[str] = [pandoc_bin, "--from", input_format, "--to", "markdown"]
        if language:
            cmd.extend(["--language", language])
        cmd.append(working_path)

        logger.debug(f"Running pandoc: {' '.join(cmd)}")

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
                logger.warning(
                    f"pandoc returned {proc.returncode} for {file_path}: {stderr.decode(errors='replace')}"
                )
                return ExtractResult(
                    text="",
                    pages_processed=0,
                    pages_total=1,
                    timed_out_pages=[1],
                    checkpoint_position=0,
                ).to_dict()

            text = stdout.decode(errors="replace")
            return ExtractResult(
                text=text,
                pages_processed=1,
                pages_total=1,
                timed_out_pages=[],
                checkpoint_position=1,
            ).to_dict()

        except FileNotFoundError:
            logger.error(f"pandoc binary '{pandoc_bin}' not found")
            raise
        except asyncio.TimeoutError:
            logger.warning(f"pandoc timed out after {RANGE_TIMEOUT_SECONDS}s for {file_path}")
            return ExtractResult(
                text="",
                pages_processed=0,
                pages_total=1,
                timed_out_pages=[1],
                checkpoint_position=0,
            ).to_dict()

    finally:
        for path in cleanup_paths:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
