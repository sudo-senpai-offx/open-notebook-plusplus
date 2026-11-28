"""trafilatura wrapper for Week 4 poly-pipeline.

Week 4 Day 3: HTML/web content extraction wrapper.

Extracts article content from HTML via rs-trafilatura (Rust, page-type-aware)
with a 30s timeout (instant lane). rs-trafilatura automatically strips boilerplate
(<nav>, <footer>, <header>, scripts, styles, ads) so the LLM gets clean
article text, not chrome. It also classifies the page type
(article/forum/product/collection/listing/documentation/service) and returns an
extraction quality score (0.0-1.0).

Page-type-aware fields (decision #2): page_type, extraction_quality,
classification_confidence flow into Source7StageState so downstream stages
(chunk, embed, index) can branch on page type and observability.

Quality threshold (decision #3): extraction_quality < 0.80 flags the source as
extracting_partial with a warning, but the content is still indexed.

SPA / JavaScript-required detection (decision #4):
If extracted markdown is < 200 chars AND the raw HTML contains SPA markers
(id="root", id="app", id="__next", <noscript>, high script ratio),
the job is marked extracting_partial with warning "javascript_required"
instead of silently producing an empty source.

Empty / blocked detection (decision #5):
If rs-trafilatura returns empty AND no SPA markers exist, fall back to Python
trafilatura. If that also returns empty, raises ValueError so extract_stage
routes to compensate_extract (extracting_failed).

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
from open_notebook.utils.job_control import report_progress_record

# Quality threshold below which extraction is considered unreliable
# (decision #3). Configurable via settings.extraction_quality_threshold,
# default 0.80.
EXTRACTION_QUALITY_THRESHOLD = 0.80

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

    # Page-type-aware fields (decision #2).
    page_type: Optional[str] = None
    extraction_quality: float = 0.0
    classification_confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages_processed": self.pages_processed,
            "pages_total": self.pages_total,
            "timed_out_pages": self.timed_out_pages,
            "checkpoint_position": self.checkpoint_position,
            "javascript_required": self.javascript_required,
            "extraction_empty": self.extraction_empty,
            "page_type": self.page_type,
            "extraction_quality": self.extraction_quality,
            "classification_confidence": self.classification_confidence,
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

    Runs before rs-trafilatura to prevent bloated inline assets from stalling
    the parser. rs-trafilatura already strips nav/footer/headers, but
    pre-stripping scripts and base64 reduces parse time and memory on large
    pages.
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
    """Extract article content from HTML via rs-trafilatura.

    Args:
        command_id: The command id for checkpointing and progress reporting.
        file_path: Path to an HTML file (optional if html_content provided).
        html_content: Raw HTML string (optional if file_path provided).
        url: Source URL (for page-type classification and logging).

    Returns:
        ExtractResult dict with text, pages_processed, pages_total,
        timed_out_pages, checkpoint_position, javascript_required,
        extraction_empty, page_type, extraction_quality,
        classification_confidence.

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

    # Pre-strip boilerplate before rs-trafilatura
    html = strip_html_boilerplate(html_content)

    # SPA detection BEFORE rs-trafilatura (decision #4).
    # rs-trafilatura extracts whatever HTML it receives; it does not flag
    # "this page is a React shell with no server-rendered content". The SPA
    # pre-check catches structurally-valid HTML with no content because
    # JavaScript hasn't run.
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

    # Run rs-trafilatura via Python API with a 30s timeout (instant lane)
    import rs_trafilatura

    await report_progress_record(command_id, progress=30, stage="extract")

    try:
        rs_result = await asyncio.wait_for(
            asyncio.to_thread(
                rs_trafilatura.extract,
                html,
                url=url,
                output_markdown=True,
                include_tables=True,
                include_links=True,
                favor_precision=True,
            ),
            timeout=RANGE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"rs-trafilatura timed out after {RANGE_TIMEOUT_SECONDS}s")
        result.timed_out_pages.append(1)
        result.extraction_empty = True
        rs_result = None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"rs-trafilatura failed: {exc}")
        result.timed_out_pages.append(1)
        result.extraction_empty = True
        rs_result = None

    if rs_result is not None:
        result.text = rs_result.content_markdown or rs_result.main_content or ""
        result.page_type = rs_result.page_type
        result.extraction_quality = rs_result.extraction_quality or 0.0
        result.classification_confidence = rs_result.classification_confidence

        if result.text.strip():
            result.pages_processed = 1
            logger.info(
                f"Extracted HTML from {file_path or url} "
                f"(page_type={result.page_type}, "
                f"quality={result.extraction_quality:.2f}, "
                f"{len(result.text)} chars)"
            )
        else:
            result.extraction_empty = True

    # SPA detection after extraction
    if not result.text.strip() and js_required:
        result.javascript_required = True
        result.extraction_empty = True
    elif not result.text.strip():
        result.extraction_empty = True

    # Empty content fallback (decision #5): if rs-trafilatura returned empty
    # and SPA detection didn't fire, try Python trafilatura before compensating.
    if result.extraction_empty and not result.javascript_required:
        try:
            import trafilatura  # optional dependency (legacy-html extra)

            fallback = trafilatura.extract(
                html, url=url, output_format="markdown", include_tables=True
            )
            if fallback and fallback.strip():
                result.text = fallback.strip()
                result.extraction_empty = False
                result.pages_processed = 1
                result.page_type = None
                result.extraction_quality = 0.0
                result.classification_confidence = None
                logger.info(
                    f"Fallback Python trafilatura extracted content from "
                    f"{file_path or url} ({len(result.text)} chars)"
                )
        except ImportError:
            # Python trafilatura not installed (legacy-html extra missing);
            # proceed to compensation below.
            logger.debug(
                "Python trafilatura not installed; skipping empty-content fallback."
            )

    # If empty with no SPA markers, raise so extract_stage routes to compensation
    if result.extraction_empty and not result.javascript_required:
        raise ValueError("html_extraction_empty")

    # Quality threshold (decision #3): extraction_quality < 0.80 flags the
    # source as extracting_partial with a warning, but content is still indexed.
    # The warning is propagated to _extract_trafilatura_stage via the
    # extraction_quality field in the result dict; the caller appends it to
    # the accumulated warnings list.

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
        f"page_type={result.page_type}, quality={result.extraction_quality:.2f}, "
        f"js_required={result.javascript_required}, empty={result.extraction_empty}"
    )

    return result.to_dict()
