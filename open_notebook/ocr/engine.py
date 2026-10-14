"""Run OCR over the parts of a document that need it.

Deliberately a subprocess and nothing more. Tesseract is a mature C++
engine with its own process model, and wrapping it in-process would mean
carrying its memory in the Python worker for the whole lifetime of the
app - on a 3 GB device, where the chat model and the embedding server are
already resident, that is the difference between working and being
killed.

Configuration is environment-first so both architectures - x86 desktop
and arm64 Termux - use the same code and the same binary name.

    OPEN_NOTEBOOK_OCR_LANG=eng+jpn     languages, Tesseract syntax
    OPEN_NOTEBOOK_OCR_BIN=tesseract    binary to exec
    OPEN_NOTEBOOK_OCR_DPI=200          rasterisation resolution
    OPEN_NOTEBOOK_OCR_ENABLED=true     master switch
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import pypdfium2 as pdfium
from loguru import logger

OCR_ENABLED_ENV = "OPEN_NOTEBOOK_OCR_ENABLED"
OCR_LANG_ENV = "OPEN_NOTEBOOK_OCR_LANG"
OCR_BIN_ENV = "OPEN_NOTEBOOK_OCR_BIN"
OCR_DPI_ENV = "OPEN_NOTEBOOK_OCR_DPI"
OCR_TIMEOUT_ENV = "OPEN_NOTEBOOK_OCR_TIMEOUT"

DEFAULT_LANG = "eng+jpn"
DEFAULT_BIN = "tesseract"

# 200 DPI is the balance point, not a compromise.
#
# Tesseract's own guidance is that text needs an x-height of at least 20
# pixels. 10pt body text is ~14pt x-height, so 200 DPI yields ~28px -
# comfortably past the threshold - while 300 DPI costs roughly 2.2x the
# pixels for no gain on type that is already clean. 300 only helps on
# small print and degraded scans, which is what the retry path is for.
DEFAULT_DPI = 200
MIN_DPI = 150
MAX_DPI = 400

DEFAULT_TIMEOUT = 300

# Page segmentation mode. 3 is "fully automatic": tesseract finds columns
# and blocks itself. It is slower than 6 but far better on the multi
# column layouts that lecture notes and papers actually use.
PSM_AUTOMATIC = "3"
# Uniform block of text - faster, and the right choice for a figure that
# is a single caption or a cropped chart.
PSM_SINGLE_BLOCK = "6"

# Confidence gate, 0-100. Tesseract scores every word.
#
# This is the difference between useful text and noise. Running it over a
# molecular diagram returns lines like "KINA i N" and "\e \'protein =" -
# technically an OCR result, and completely wrong. Those words score in
# the twenties. Real prose scores 85-95. Dropping anything below
# MIN_WORD_CONFIDENCE removes the diagrams' gibberish while keeping the
# surrounding prose intact.
MIN_WORD_CONFIDENCE = 70

# A page whose surviving words still average below this is not a page of
# text at all - it is a figure that happened to contain letter-like
# shapes. Its output is discarded entirely rather than stored, because
# garbage in full_text poisons embeddings and search results in ways that
# are hard to trace back here.
MIN_PAGE_MEAN_CONFIDENCE = 60


def ocr_enabled() -> bool:
    return os.getenv(OCR_ENABLED_ENV, "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def ocr_binary() -> str:
    return os.getenv(OCR_BIN_ENV, DEFAULT_BIN).strip() or DEFAULT_BIN


def ocr_languages() -> str:
    """The language set, validated to Tesseract's own syntax.

    Only ``+``-joined three-letter codes are accepted. A malformed list
    makes tesseract exit non-zero with an error on stderr, which is a
    poor way to learn that someone typed ``en-jp``.
    """
    raw = os.getenv(OCR_LANG_ENV, DEFAULT_LANG).strip() or DEFAULT_LANG
    parts = [p.strip() for p in raw.replace(",", "+").split("+") if p.strip()]
    codes = [p for p in parts if p.isalpha() and 2 <= len(p) <= 3]
    return "+".join(codes) if codes else "eng"


def ocr_dpi() -> int:
    raw = os.getenv(OCR_DPI_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_DPI
    except ValueError:
        return DEFAULT_DPI
    return max(MIN_DPI, min(MAX_DPI, value))


def ocr_timeout() -> int:
    raw = os.getenv(OCR_TIMEOUT_ENV, "").strip()
    try:
        return max(30, int(raw)) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def available() -> tuple[bool, str]:
    """Whether OCR can run here, and why not if it cannot."""
    if not ocr_enabled():
        return False, f"{OCR_ENABLED_ENV} is off"
    path = shutil.which(ocr_binary())
    if not path:
        return False, (
            f"{ocr_binary()} is not on PATH. On Termux: pkg install tesseract. "
            "OCR stays off until then; everything else works."
        )
    return True, path


@dataclass
class OCRResult:
    text: str
    pages: int
    seconds: float
    cached: bool = False
    error: Optional[str] = None
    #: Pages rendered but discarded as too uncertain to be text.
    skipped_pages: int = 0
    mean_confidence: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


# Rendered pages are cached by content, so re-running OCR after an
# unrelated change - or after the blob arrives on a second device - does
# not pay for recognition twice.
_CACHE_DIR = Path(
    os.getenv("OPEN_NOTEBOOK_OCR_CACHE", "").strip()
    or (Path(os.getenv("DATA_FOLDER", "./data")) / "ocr-cache")
)


def _cache_key(pdf_path: Path, page: int, dpi: int, lang: str, psm: str) -> str:
    """Key on the *file's* identity plus the settings that affect output."""
    try:
        stat = pdf_path.stat()
        fingerprint = f"{pdf_path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        fingerprint = pdf_path.name
    raw = f"{fingerprint}:{page}:{dpi}:{lang}:{psm}".encode()
    return hashlib.sha256(raw).hexdigest()


def _run_tesseract(image_path: Path, lang: str, psm: str) -> tuple[str, float, str]:
    """Return (text, mean confidence, error).

    Writes the TSV to a file rather than to stdout. Piping tesseract's
    TSV emits the confidence column but silently empties the text column,
    so every page looks like it had no recognisable words and the whole
    library OCR'd to nothing. Given an output base, tesseract writes
    <base>.tsv correctly - verified: 22 words at confidence 88-97 versus
    zero over a pipe.

    The engine is a subprocess by design; see the module docstring.
    """
    with tempfile.TemporaryDirectory(prefix="ocr-out-") as out_dir:
        base = Path(out_dir) / "page"
        cmd = [
            ocr_binary(),
            str(image_path),
            str(base),
            "-l",
            lang,
            "--psm",
            psm,
            "tsv",
        ]
        env = dict(os.environ)
        # Tesseract spawns its own OpenMP pool. On a device already
        # running two llama-servers, letting it also claim every core is
        # how the whole app gets killed by the low-memory killer.
        env["OMP_THREAD_LIMIT"] = "1"
        try:
            proc = subprocess.run(  # noqa: S603 - binary from config, not a request
                cmd,
                capture_output=True,
                text=True,
                timeout=ocr_timeout(),
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return "", 0.0, f"{ocr_binary()} not found"
        except subprocess.TimeoutExpired:
            return "", 0.0, f"tesseract timed out after {ocr_timeout()}s"
        if proc.returncode != 0:
            return "", 0.0, (proc.stderr or "").strip()[:300]

        tsv_path = base.with_suffix(".tsv")
        if not tsv_path.is_file():
            return (
                "",
                0.0,
                f"tesseract wrote no TSV (stderr: {proc.stderr.strip()[:120]})",
            )
        try:
            tsv = tsv_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return "", 0.0, f"cannot read tesseract output: {exc}"

    text, confidence = _tsv_to_text(tsv)
    return text, confidence, ""


def _tsv_to_text(tsv: str) -> tuple[str, float]:
    """Keep only the words tesseract is confident about.

    TSV is tab-separated with a header; the 12th column is the word's
    confidence and the 13th is its text. Anything below
    MIN_WORD_CONFIDENCE is discarded, and the mean of what survives is
    returned so the caller can drop a page that is really a figure.
    """
    kept: list[tuple[int, int, int, str]] = []
    confidences: list[float] = []
    lines = tsv.splitlines()
    for index, line in enumerate(lines[1:], start=1):
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        # Confidence is a float ("92.167656"). Parsing it as an int
        # raises, and the except silently drops the word - so every real
        # word was discarded and only whitespace rows survived, making
        # the entire library OCR to nothing while looking like a
        # confidence-filtering result.
        try:
            confidence = float(parts[10])
        except ValueError:
            continue
        if confidence < 0:
            continue
        word = parts[11].strip()
        if not word:
            continue
        try:
            block, par, line_no = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        if confidence < MIN_WORD_CONFIDENCE:
            continue
        kept.append((block, par, line_no, word))
        confidences.append(confidence)

    if not kept:
        return "", 0.0

    mean = sum(confidences) / len(confidences)
    if mean < MIN_PAGE_MEAN_CONFIDENCE:
        return "", mean

    # Rebuild paragraphs from the block/paragraph/line grouping so the
    # result reads like prose rather than a bag of words.
    text_lines: list[str] = []
    previous: tuple[int, int, int] | None = None
    for block, par, line_no, word in kept:
        group = (block, par, line_no)
        if previous is not None and group != previous:
            text_lines.append("")
        text_lines.append(word)
        previous = group
    return " ".join(text_lines).replace("  ", " ").strip(), mean


def _is_whole_page(
    region: Tuple[float, float, float, float],
    document: "pdfium.PdfDocument",
    page_index: int,
) -> bool:
    """True when the region covers the page, within a rounding tolerance."""
    try:
        width, height = document[page_index].get_size()
    except Exception:
        return False
    tolerance = 2.0
    return (
        region[0] <= tolerance
        and region[1] <= tolerance
        and region[2] >= width - tolerance
        and region[3] >= height - tolerance
    )


def _render_region(
    document: "pdfium.PdfDocument",
    page_index: int,
    region: Optional[Tuple[float, float, float, float]],
    dpi: int,
) -> Optional[Path]:
    """Rasterise a page or a region of it to a temporary PNG."""
    page = document[page_index]
    target = page.render(scale=dpi / 72.0)
    bitmap = target.to_pil()

    if region is not None:
        scale = dpi / 72.0
        x0, y0, x1, y1 = region
        # PDF points have their origin at the bottom-left; the raster has
        # its at the top-left. Without this flip, a crop returns the wrong
        # part of the page - which reads as "OCR produced nonsense".
        left = max(0, int(x0 * scale))
        right = min(bitmap.width, int(x1 * scale))
        top = max(0, int((page.get_height() - y1) * scale))
        bottom = min(bitmap.height, int((page.get_height() - y0) * scale))
        if right - left < 16 or bottom - top < 16:
            return None
        bitmap = bitmap.crop((left, top, right, bottom))

    handle = tempfile.NamedTemporaryFile(prefix="ocr-", suffix=".png", delete=False)
    try:
        bitmap.save(handle.name, format="PNG", optimize=False, compress_level=1)
    finally:
        handle.close()
    return Path(handle.name)


def ocr_document(
    path: Path | str,
    analysis: Optional[object] = None,
    dpi: Optional[int] = None,
    lang: Optional[str] = None,
) -> OCRResult:
    """Read only the pages and regions the analysis flagged.

    Returns an empty result rather than raising when OCR is unavailable:
    a source with no text layer should still be importable, just without
    extracted text, and failing the whole ingest would be worse.
    """
    started = time.monotonic()
    pdf_path = Path(path)

    ok, detail = available()
    if not ok:
        return OCRResult(text="", pages=0, seconds=0.0, error=detail)
    if not pdf_path.is_file():
        return OCRResult(text="", pages=0, seconds=0.0, error="file not found")

    from open_notebook.ocr.analyze import analyse

    doc_analysis = analysis if analysis is not None else analyse(pdf_path)
    if not getattr(doc_analysis, "needs_ocr", False):
        return OCRResult(text="", pages=0, seconds=time.monotonic() - started)

    resolution = dpi or ocr_dpi()
    languages = lang or ocr_languages()

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        return OCRResult(text="", pages=0, seconds=0.0, error=f"cannot open PDF: {exc}")

    chunks: List[str] = []
    rendered = 0
    cached = 0
    skipped_low_confidence = 0
    total_confidence = 0.0
    try:
        for page_analysis in doc_analysis.ocr_pages:  # type: ignore[attr-defined]
            for index, region in enumerate(page_analysis.regions):
                key = _cache_key(
                    pdf_path, page_analysis.number, resolution, languages, PSM_AUTOMATIC
                )
                cache_file = _CACHE_DIR / f"{key}.txt"
                if cache_file.is_file():
                    text = cache_file.read_text(encoding="utf-8", errors="replace")
                    if text.strip():
                        chunks.append(text)
                        cached += 1
                        continue

                psm = (
                    PSM_AUTOMATIC
                    if len(page_analysis.regions) > 1
                    else PSM_SINGLE_BLOCK
                )
                image = _render_region(
                    document, page_analysis.number, region, resolution
                )
                if image is None:
                    continue
                try:
                    text, confidence, error = _run_tesseract(image, languages, psm)
                    if error:
                        logger.warning(f"OCR page {page_analysis.number + 1}: {error}")
                        continue
                    text = text.strip()
                    if text:
                        chunks.append(text)
                        rendered += 1
                        total_confidence += confidence
                        try:
                            cache_file.write_text(text, encoding="utf-8")
                        except OSError:
                            pass
                    else:
                        # Read but discarded: a figure, not a page of
                        # text. Better no text than confident nonsense.
                        skipped_low_confidence += 1
                finally:
                    image.unlink(missing_ok=True)
    finally:
        try:
            document.close()
        except Exception:
            pass

    return OCRResult(
        text="\n\n".join(chunks),
        pages=rendered + cached,
        seconds=time.monotonic() - started,
        cached=cached > 0 and rendered == 0,
        skipped_pages=skipped_low_confidence,
        mean_confidence=(total_confidence / rendered) if rendered else 0.0,
    )


def probe(path: Path | str) -> dict:
    """Describe what OCR *would* do, without doing it."""
    from open_notebook.ocr.analyze import analyse

    ok, detail = available()
    result = analyse(Path(path))
    summary = result.summary()
    summary["available"] = ok
    summary["detail"] = detail
    summary["languages"] = ocr_languages()
    summary["dpi"] = ocr_dpi()
    return summary
