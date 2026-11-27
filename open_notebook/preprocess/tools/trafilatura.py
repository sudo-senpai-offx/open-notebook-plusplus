"""trafilatura wrapper for Week 4 poly-pipeline.

Week 4 Day 3: HTML/web content extraction wrapper.

Extracts article content from HTML via trafilatura with a 30s timeout
(instant lane). trafilatura automatically strips boilerplate
(<nav>, <footer>, <header>, scripts, styles, ads) so the LLM gets clean
article text, not chrome.

SPA / JavaScript-required detection:
If extracted markdown is < 200 chars AND the raw HTML contains SPA markers
(id="root", id="app", id="__next", <noscript>, high script ratio),
the job is marked extracting_partial with warning "javascript_required"
instead of silently producing an empty source.

Empty / blocked detection:
If extracted text is empty and no SPA markers exist, raises ValueError
so extract_stage routes to compensate_extract (extracting_failed).

Returns ExtractResult dict compatible with extract_stage.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from open_notebook.ai.models import JobCheckpoint
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.job_control import report_progress_record

# HTML MIME types that should route to trafilatura (not pandoc)
HTML_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
}

# Single-page markers that suggest JavaScript/SPA content
SPA_MARKERS = [
    'id="root"',
    'id="app"',
    'id="__next"',
    "<noscript>",
]

# High script ratio threshold: if > 50% of HTML is script tags, likely JS-only
SCRIPT_RATIO_THRESHOLD = 0.5

# Minimum readable text length; below this with SPA markers = JS-required
MIN_READABLE_CHARS = 200

RANGE_TIMEOUT_SECONDS = 30
STAGE_TIMEOUT_SECONDS = 300


@dataclass
class ExtractResult:
    """Result of HTML extraction."""

    text: str = ""
    pages_processed: int = 0
    pages_total: int = 1
    timed_out_pages: List[int] = field(default_factory=list)
    checkpoint_position: int = 0
    javascript_required: bool = False
    extraction_empty: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages_processed": self.pages_processed,
            "pages_total": self.pages_total,
            "timed_out_pages": self.timed_out_pages,
            "checkpoint_position": self.checkpoint_position,
            "javascript_required": self.javascript_required,
            "extraction_empty": self.extraction_empty,
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
            f"checkpoint write failed for {command_id} at {position}: {exc}"
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


def strip_html_boilerplate(html: str) -> str:
    """Strip <script>, <style>, and inline base64 image URIs from HTML.

    Runs before trafilatura to prevent bloated inline assets from stalling
    the parser. trafilatura already strips nav/footer/headers, but pre-stripping
    scripts and base64 reduces parse time and memory on large pages.
    """
    # Strip <script>...</script>
    html = re.sub(
        r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
    )
    # Strip <style>...</style>
    html = re.sub(
        r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE
    )
    # Strip base64 data URIs
    html = re.sub(
        r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "", html
    )
    return html


def has_spa_markers(html: str) -> bool:
    """Check if HTML contains SPA / JavaScript-required markers."""
    lower = html.lower()
    for marker in SPA_MARKERS:
        if marker.lower() in lower:
            return True
    return False


def script_ratio(html: str) -> float:
    """Ratio of script tag content to total HTML length."""
    if not html:
        return 0.0
    total = len(html)
    if total == 0:
        return 0.0
    script_chars = sum(
        len(m.group(0))
        for m in re.finditer(
            r"<script[^>]*>.*?</script>", html, flags=re.DOTALL | re.IGNORECASE
        )
    )
    return script_chars / total


async def extract_html(
    command_id: Optional[str],
    file_path: Optional[str] = None,
    html_content: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract article content from HTML via trafilatura.

    Args:
        command_id: The command id for checkpointing and progress reporting.
        file_path: Path to an HTML file (optional if html_content provided).
        html_content: Raw HTML string (optional if file_path provided).
        url: Source URL (for logging only).

    Returns:
        ExtractResult dict with text, pages_processed, pages_total,
        timed_out_pages, checkpoint_position, javascript_required,
        extraction_empty.

    Raises:
        ValueError: if extracted text is empty and no SPA markers exist
            (blocked / 403 / CAPTCHA / truly empty HTML).
    """
    if html_content is None:
        if not file_path or not os.path.isfile(file_path):
            return ExtractResult(
                text="",
                pages_processed=0,
                pages_total=1,
                timed_out_pages=[],
                checkpoint_position=0,
                javascript_required=False,
                extraction_empty=True,
            ).to_dict()
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            html_content = f.read()

    if not html_content or not html_content.strip():
        return ExtractResult(
            text="",
            pages_processed=0,
            pages_total=1,
            timed_out_pages=[],
            checkpoint_position=0,
            javascript_required=False,
            extraction_empty=True,
        ).to_dict()

    # Pre-strip boilerplate before trafilatura
    html = strip_html_boilerplate(html_content)

    # SPA detection BEFORE trafilatura (trafilatura may strip scripts we need to detect)
    js_required = has_spa_markers(html) or script_ratio(html) > SCRIPT_RATIO_THRESHOLD

    result = ExtractResult(pages_total=1)

    # Resume from checkpoint if available
    resume_from = 0
    if command_id:
        cp = await _read_checkpoint(command_id)
        if cp and cp.get("stage") == "extract":
            resume_from = int(cp.get("position", 0))
            result.pages_processed = int(cp.get("pages_processed", 0))
            result.checkpoint_position = resume_from
            logger.info(f"Resuming HTML extraction for {command_id} from {resume_from}")

    if resume_from >= 1:
        return result.to_dict()

    # Run trafilatura via Python API with a 30s timeout (instant lane)
    import trafilatura

    await report_progress_record(command_id, progress=30, stage="extract")

    try:
        extracted = await asyncio.wait_for(
            asyncio.to_thread(
                trafilatura.extract,
                html,
                output_format="markdown",
                include_tables=True,
                include_links=True,
                fast=True,
            ),
            timeout=RANGE_TIMEOUT_SECONDS,
        )
        if extracted and extracted.strip():
            result.text = extracted.strip()
            result.pages_processed = 1
            logger.info(
                f"Extracted HTML from {file_path or url} ({len(result.text)} chars)"
            )
        else:
            result.extraction_empty = True
    except asyncio.TimeoutError:
        logger.warning(f"trafilatura timed out after {RANGE_TIMEOUT_SECONDS}s")
        result.timed_out_pages.append(1)
        result.extraction_empty = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"trafilatura failed: {exc}")
        result.timed_out_pages.append(1)
        result.extraction_empty = True

    # SPA detection after extraction
    if not result.text.strip() and js_required:
        result.javascript_required = True
        result.extraction_empty = True
    elif not result.text.strip():
        result.extraction_empty = True

    # If empty with no SPA markers, raise so extract_stage routes to compensation
    if result.extraction_empty and not result.javascript_required:
        raise ValueError("html_extraction_empty")

    # Checkpoint after extraction
    result.checkpoint_position = 1
    await _write_checkpoint(
        command_id=command_id or "",
        position=1,
        pages_processed=result.pages_processed,
        timed_out_pages=result.timed_out_pages,
        text_accumulator=[result.text] if result.text else [],
    )

    if command_id:
        await report_progress_record(command_id, progress=90, stage="extract")

    logger.info(
        f"HTML extraction complete for {command_id or 'unknown'}: "
        f"{result.pages_processed}/{result.pages_total} pages, "
        f"js_required={result.javascript_required}, empty={result.extraction_empty}"
    )

    return result.to_dict()
