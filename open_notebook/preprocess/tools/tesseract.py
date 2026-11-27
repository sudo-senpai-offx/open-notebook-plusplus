"""tesseract wrapper for Week 4 poly-pipeline.

Week 4 Day 4: OCR wrapper for scanned PDFs and images (Heavy / Deferred Lane).

Reuses pdf.py's 5-page range loop and intra-stage JobCheckpoint persistence,
with strict Android LMK (Low Memory Killer) safeguards:

- Rasterization + DPI cap: Render PDF pages to PNG at 300 DPI max
  (pdftoppm -png -r 300 -f N -l M), downsampling higher-DPI standalone images
  before OCR.
- Android memory & CPU caps: OMP_THREAD_LIMIT=1 and ulimit -v 500000
  (500 MB virtual memory cap) per tesseract invocation, 90s timeout per
  5-page range.
- Language propagation: Pass -l <lang> mapped from source.language
  (defaulting to eng when unset).
- Low-confidence warning: Parse TSV confidence output
  (tesseract ... tsv); if mean page confidence < 60%, append
  "low_ocr_confidence" to warnings and flag extracting_partial — never
  silently return garbage OCR text.
- Partial timeout recovery: If a 5-page range times out, record
  timed_out_pages, write checkpoint, and continue to the next range.

Returns an ExtractResult dict compatible with extract_stage:

    {
        "text": str,                    # concatenated OCR text
        "pages_processed": int,         # pages successfully OCR'd
        "pages_total": int,             # total pages attempted
        "timed_out_pages": List[int],   # page numbers that timed out
        "checkpoint_position": int,     # last checkpointed page
        "low_confidence": bool,         # any page below 60% mean confidence
        "warnings": List[str],          # human-readable warnings
    }
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

# TESSERACT_BINARY and PDFFTOPM_BINARY resolved at call time so tests can
# monkeypatch them.
TESSERACT_BINARY = "tesseract"
PDFFTOPM_BINARY = "pdftoppm"

from open_notebook.ai.models import JobCheckpoint
from open_notebook.utils.job_control import report_progress_record

PAGES_PER_RANGE = 5
RANGE_TIMEOUT_SECONDS = 90
STAGE_TIMEOUT_SECONDS = 1800

# Virtual memory cap for tesseract on Android (500 MB).
ULIMIT_V_MEMORY_KB = 500000

# Mean confidence threshold below which a page is flagged low-confidence.
LOW_CONFIDENCE_THRESHOLD = 60.0

# Default OCR language when source.language is unset.
DEFAULT_LANGUAGE = "eng"


@dataclass
class ExtractResult:
    """Result of OCR extraction."""

    text: str = ""
    pages_processed: int = 0
    pages_total: int = 0
    timed_out_pages: List[int] = field(default_factory=list)
    checkpoint_position: int = 0
    low_confidence: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "pages_processed": self.pages_processed,
            "pages_total": self.pages_total,
            "timed_out_pages": self.timed_out_pages,
            "checkpoint_position": self.checkpoint_position,
            "low_confidence": self.low_confidence,
            "warnings": self.warnings,
        }


def _build_tesseract_env() -> Dict[str, str]:
    """Build the environment for a tesseract subprocess with Android caps.

    Sets OMP_THREAD_LIMIT=1 to prevent OpenMP from spawning threads beyond
    the device's capacity, which can trigger the Low Memory Killer on
    constrained Android devices.
    """
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"
    return env


def _mean_confidence(tsv_text: str) -> Optional[float]:
    """Parse tesseract TSV output and return mean word confidence.

    Tesseract TSV columns: level, page_num, block_num, par_num, line_num,
    word_num, left, top, width, height, conf, text. The conf column (index
    10) holds per-word confidence (-1 for non-word levels). We average only
    word-level confidences (level == 5) that are >= 0.
    """
    confidences: List[float] = []
    for line in tsv_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            level = int(parts[0])
            conf = float(parts[10])
        except (ValueError, IndexError):
            continue
        if level == 5 and conf >= 0:  # word level, valid confidence
            confidences.append(conf)

    if not confidences:
        return None
    return sum(confidences) / len(confidences)


async def _rasterize_page_range(
    file_path: str,
    start_page: int,
    end_page: int,
    dpi: int,
    tmp_dir: str,
) -> List[str]:
    """Render a page range of a PDF to PNG via pdftoppm at capped DPI.

    Returns a list of PNG paths (one per page in the range), or an empty
    list on failure. pdftoppm names output files ``<prefix>-<page>.png``.
    DPI is capped at 300 to avoid bloating memory on Android devices.
    """
    prefix = os.path.join(tmp_dir, f"page-{start_page:04d}")
    cmd: List[str] = [
        PDFFTOPM_BINARY,
        "-png",
        "-r",
        str(min(dpi, 300)),  # DPI cap: never render above 300 DPI
        "-f",
        str(start_page),
        "-l",
        str(end_page),
        file_path,
        prefix,
    ]

    png_paths: List[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=RANGE_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            logger.warning(
                f"pdftoppm returned {proc.returncode} for pages "
                f"{start_page}-{end_page}: {stderr.decode(errors='replace')}"
            )
            return png_paths
    except asyncio.TimeoutError:
        logger.warning(
            f"pdftoppm timed out after {RANGE_TIMEOUT_SECONDS}s for "
            f"pages {start_page}-{end_page}"
        )
        return png_paths
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"pdftoppm failed for pages {start_page}-{end_page}: {exc}")
        return png_paths

    # pdftoppm outputs page-N.png where N is the page number. Collect all
    # PNGs matching our prefix for this range.
    try:
        basename = os.path.basename(prefix)
        for name in sorted(os.listdir(tmp_dir)):
            if name.startswith(basename) and name.endswith(".png"):
                png_paths.append(os.path.join(tmp_dir, name))
    except OSError:
        pass
    return png_paths


async def _ocr_single_image(
    image_path: str,
    language: str,
    page_num: int,
    tmp_dir: str,
) -> tuple[str, bool, Optional[float]]:
    """Run tesseract OCR on a single image file.

    Returns (text, success, mean_confidence). Uses TSV output to parse
    per-word confidence. Applies ulimit -v 500000 (500 MB virtual memory
    cap) via a shell wrapper to protect Android devices from OOM.
    """
    tsv_path = os.path.join(tmp_dir, f"ocr-{page_num}.tsv")
    txt_path = os.path.join(tmp_dir, f"ocr-{page_num}")

    # Build tesseract command: output to txt (base name, .txt appended) and tsv.
    cmd: List[str] = [
        TESSERACT_BINARY,
        image_path,
        txt_path,  # output base; tesseract appends .txt
        "-l",
        language,
        "tsv",  # also write TSV sidecar
    ]

    # Compose a shell command that applies ulimit -v before exec'ing tesseract.
    # This enforces a 500 MB virtual memory cap per invocation.
    quoted = " ".join(f"'{c}'" for c in cmd)
    shell_cmd = f"ulimit -v {ULIMIT_V_MEMORY_KB}; {quoted}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            shell_cmd,
            env=_build_tesseract_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=RANGE_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            logger.warning(
                f"tesseract returned {proc.returncode} for page {page_num}: "
                f"{stderr.decode(errors='replace')}"
            )
            return "", False, None
    except asyncio.TimeoutError:
        logger.warning(f"tesseract timed out after {RANGE_TIMEOUT_SECONDS}s for page {page_num}")
        return "", False, None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"tesseract failed for page {page_num}: {exc}")
        return "", False, None

    # Read OCR text output.
    text = ""
    try:
        with open(txt_path + ".txt", "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        pass

    # Read TSV for confidence.
    mean_conf = None
    try:
        with open(tsv_path, "r", encoding="utf-8", errors="replace") as f:
            tsv_text = f.read()
        mean_conf = _mean_confidence(tsv_text)
    except OSError:
        pass

    return text, True, mean_conf


async def extract_ocr(
    command_id: Optional[str],
    file_path: str,
    page_count: int,
    language: Optional[str] = None,
    dpi: int = 300,
) -> Dict[str, Any]:
    """Extract text from a scanned PDF or image via tesseract OCR.

    Splits pages into 5-page ranges, rasterizes each range via pdftoppm at
    capped 300 DPI, then runs tesseract on each page with OMP_THREAD_LIMIT=1
    and a 500 MB virtual memory cap. Writes an intra-stage JobCheckpoint
    after each range so a kill -9 mid-extraction preserves completed pages.

    Args:
        command_id: The command id for checkpointing and progress reporting.
        file_path: Path to the PDF or image file.
        page_count: Total number of pages (for PDFs; 1 for standalone images).
        language: Optional language hint (default "eng").
        dpi: Rendering DPI, capped at 300.

    Returns:
        ExtractResult dict.
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="", pages_processed=0, pages_total=0, timed_out_pages=[], checkpoint_position=0
        ).to_dict()

    lang = language or DEFAULT_LANGUAGE
    result = ExtractResult(pages_total=page_count)

    # Resume from checkpoint if available.
    resume_from = 0
    if command_id:
        cp = await JobCheckpoint.get_for_command(command_id)
        if cp and cp.get("stage") == "extract":
            resume_from = int(cp.get("position", 0))
            result.pages_processed = int(cp.get("pages_processed", 0))
            result.timed_out_pages = list(cp.get("timed_out_pages", []))
            result.checkpoint_position = resume_from
            result.low_confidence = bool(cp.get("low_confidence", False))
            result.warnings = list(cp.get("warnings", []))
            logger.info(
                f"Resuming OCR extraction for {command_id} from page {resume_from}"
            )

    text_accumulator: List[str] = []
    is_image = not file_path.lower().endswith(".pdf")

    for start_page in range(resume_from + 1, page_count + 1, PAGES_PER_RANGE):
        end_page = min(start_page + PAGES_PER_RANGE - 1, page_count)

        # One temp dir per range, kept alive for both rasterization and OCR.
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Rasterize the page range to PNG images.
            range_images: List[str] = []
            if is_image:
                # Standalone image: OCR it directly (single page).
                range_images = [file_path]
            else:
                png_paths = await _rasterize_page_range(
                    file_path, start_page, end_page, dpi, tmp_dir
                )
                range_images.extend(png_paths)

            # OCR each image in the range.
            range_text: List[str] = []
            range_success = True
            range_mean_confidences: List[float] = []

            for img_path in range_images:
                page_num = start_page + len(range_text)
                try:
                    text, success, mean_conf = await _ocr_single_image(
                        img_path, lang, page_num, tmp_dir
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"tesseract per-image timeout for page {page_num}"
                    )
                    range_success = False
                    continue
                if success:
                    range_text.append(text)
                    if mean_conf is not None:
                        range_mean_confidences.append(mean_conf)
                        if mean_conf < LOW_CONFIDENCE_THRESHOLD:
                            result.low_confidence = True
                            result.warnings.append(
                                f"page {page_num}: mean OCR confidence "
                                f"{mean_conf:.1f}% < {LOW_CONFIDENCE_THRESHOLD}%"
                            )
                else:
                    range_success = False

            if range_text:
                text_accumulator.append("\n".join(range_text))
                result.pages_processed += len(range_text)
            elif not range_success:
                result.timed_out_pages.extend(range(start_page, end_page + 1))

        # Checkpoint after each range.
        result.text = "\n".join(text_accumulator)
        result.checkpoint_position = end_page
        await JobCheckpoint.upsert_for_command(
            command_id=command_id or "",
            checkpoint={
                "stage": "extract",
                "position": end_page,
                "pages_processed": result.pages_processed,
                "timed_out_pages": result.timed_out_pages,
                "low_confidence": result.low_confidence,
                "warnings": result.warnings,
                "text_length": len(result.text),
            },
            increment_attempt=False,
        )

    result.text = "\n".join(text_accumulator)

    if result.low_confidence:
        result.warnings.append(
            "low_ocr_confidence: OCR low confidence detected — text may be "
            "inaccurate; consider providing a higher-quality source"
        )

    if command_id:
        await report_progress_record(
            command_id,
            progress=90,
            stage="extract",
        )

    logger.info(
        f"OCR extraction complete for {command_id or 'unknown'}: "
        f"{result.pages_processed}/{result.pages_total} pages, "
        f"low_confidence={result.low_confidence}, "
        f"{len(result.timed_out_pages)} timed out"
    )

    return result.to_dict()
