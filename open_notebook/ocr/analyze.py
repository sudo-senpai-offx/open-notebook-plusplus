"""Decide which parts of a document are worth reading with OCR.

The premise is that OCR is expensive - CPU, time, and on a phone both -
so it should not run over a document that already has readable text. It
should also not run over the *whole* of a mixed document, because most of
a research paper is typeset text that pdfplumber extracts perfectly and
that OCR would only make worse.

So every page is classified from what the PDF itself says, not from what
OCR thinks:

  TEXT       a real text layer, little imagery - already extracted, skip
  GRAPHICAL  no usable text - a scan or a figure, OCR the page
  MIXED      both - OCR only the regions occupied by images

Reading the page's object list is very cheap: it is metadata pdfium
already parsed, no rendering and no recognition. A 400-page document is
classified in milliseconds, and only the handful of pages that qualify
are ever rasterised.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from loguru import logger

# A page needs at least this many characters of real text layer before we
# trust that it has been typeset rather than scanned. 200 characters is
# roughly a paragraph - below that, a page is either a scanned image or
# a near-empty divider, and OCR is worth the cost.
MIN_TEXT_CHARS = 200

# Images covering at least this fraction of the page make it graphical.
# 0.15 is deliberately low: a single full-width figure in a paper is
# exactly the case worth reading, and a scanned page is usually above 0.9.
GRAPHICAL_AREA_RATIO = 0.15

# Below this, a page is not worth OCR'ing at all - a logo, a rule, a
# page number. Recognising it costs more than it yields.
MIN_REGION_AREA_RATIO = 0.02

# Regions closer together than this (as a fraction of page size) are
# merged. A figure drawn as forty vector rectangles is one image, and
# OCR'ing each piece separately would be both slower and worse.
MERGE_GAP_RATIO = 0.02


class PageKind(enum.Enum):
    TEXT = "text"
    GRAPHICAL = "graphical"
    MIXED = "mixed"


@dataclass
class PageAnalysis:
    """What one page is, and where its unreadable parts are."""

    number: int
    kind: PageKind
    text_chars: int
    image_area_ratio: float
    #: Bounding boxes worth OCR'ing, in PDF points, as (x0, y0, x1, y1).
    regions: List[Tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def needs_ocr(self) -> bool:
        return self.kind is not PageKind.TEXT and bool(self.regions)


@dataclass
class DocumentAnalysis:
    path: Path
    pages: List[PageAnalysis] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ocr_pages(self) -> List[PageAnalysis]:
        """Pages worth OCR'ing, in document order."""
        return [p for p in self.pages if p.needs_ocr]

    @property
    def needs_ocr(self) -> bool:
        return bool(self.ocr_pages)

    @property
    def readable(self) -> bool:
        """False when the file could not be opened as a PDF at all."""
        return self.error is None

    def summary(self) -> dict:
        counts = {k: 0 for k in PageKind}
        for page in self.pages:
            counts[page.kind] += 1
        return {
            "readable": self.readable,
            "error": self.error,
            "total_pages": len(self.pages),
            "text_pages": counts[PageKind.TEXT],
            "graphical_pages": counts[PageKind.GRAPHICAL],
            "mixed_pages": counts[PageKind.MIXED],
            "pages_to_ocr": len(self.ocr_pages),
            "regions_to_ocr": sum(len(p.regions) for p in self.ocr_pages),
        }


def _merge_regions(
    boxes: Sequence[Tuple[float, float, float, float]], page_w: float, page_h: float
) -> List[Tuple[float, float, float, float]]:
    """Coalesce nearby boxes into the regions a human would call figures.

    A diagram is frequently a handful of images and a vector path rather
    than one image, and OCR'ing each fragment separately costs a render
    and a tesseract startup per fragment for a worse result than reading
    them together.
    """
    if not boxes:
        return []
    gap_x = page_w * MERGE_GAP_RATIO
    gap_y = page_h * MERGE_GAP_RATIO

    ordered = sorted(boxes, key=lambda b: (b[1], b[0]))
    merged: List[List[float]] = []
    for box in ordered:
        x0, y0, x1, y1 = box
        for current in merged:
            # Overlap, or close enough that the gap is rasterisation noise.
            if not (
                x0 > current[2] + gap_x
                or x1 < current[0] - gap_x
                or y0 > current[3] + gap_y
                or y1 < current[1] - gap_y
            ):
                current[0] = min(current[0], x0)
                current[1] = min(current[1], y0)
                current[2] = max(current[2], x1)
                current[3] = max(current[3], y1)
                break
        else:
            merged.append([x0, y0, x1, y1])

    page_area = max(page_w * page_h, 1.0)
    return [
        (m[0], m[1], m[2], m[3])
        for m in merged
        if ((m[2] - m[0]) * (m[3] - m[1])) / page_area >= MIN_REGION_AREA_RATIO
    ]


def _analyse_page(page: "pdfium.PdfPage", number: int) -> PageAnalysis:
    width, height = page.get_size()
    page_area = max(width * height, 1.0)

    # Real character count, not a count of text objects. A typeset page
    # is typically five or six text objects no matter how much text it
    # holds, so object count says almost nothing: the first version of
    # this classified 77% of a real library as needing OCR, because every
    # page scored 5 against a threshold of 200.
    text_chars = 0
    try:
        textpage = page.get_textpage()
        text_chars = int(textpage.count_chars())
    except Exception:
        # Without a usable text layer, treat the page as carrying no text
        # and let the image/path evidence below classify it.
        text_chars = 0

    image_boxes: List[Tuple[float, float, float, float]] = []
    for obj in page.get_objects():
        try:
            kind = obj.type
        except Exception:
            continue
        if kind == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
            try:
                image_boxes.append(tuple(obj.get_bounds()))  # type: ignore[arg-type]
            except Exception:
                continue

    # Coverage from the union of image boxes rather than their sum, so
    # overlapping images cannot read as more than one whole page.
    image_area_ratio = (
        min(_union_area(image_boxes) / page_area, 1.0) if image_boxes else 0.0
    )

    if text_chars < MIN_TEXT_CHARS:
        # No usable text. A scan, a photograph, or vector art - all
        # unreadable to the text extractor. Read the whole page: on a
        # scan there is one image covering everything, and cropping to a
        # stray logo would throw the content away.
        return PageAnalysis(
            number=number,
            kind=PageKind.GRAPHICAL,
            text_chars=text_chars,
            image_area_ratio=image_area_ratio,
            regions=[(0.0, 0.0, width, height)],
        )

    if image_area_ratio < GRAPHICAL_AREA_RATIO:
        return PageAnalysis(
            number=number,
            kind=PageKind.TEXT,
            text_chars=text_chars,
            image_area_ratio=image_area_ratio,
            regions=[],
        )

    # Typeset text *and* substantial imagery: the text is already
    # extracted, so only the figures are worth reading. This is the case
    # that makes selective OCR pay for itself - a research paper where
    # OCR'ing the whole document would be mostly wasted CPU.
    return PageAnalysis(
        number=number,
        kind=PageKind.MIXED,
        text_chars=text_chars,
        image_area_ratio=image_area_ratio,
        regions=_merge_regions(image_boxes, width, height),
    )


def _union_area(boxes: Sequence[Tuple[float, float, float, float]]) -> float:
    """Exact union area of axis-aligned boxes.

    Implemented by coordinate compression: collect every distinct edge,
    then test each resulting cell. Exact for rectangles and cheap at the
    scale involved - a page has a handful of image boxes, not hundreds.

    The previous version summed x-extents only, which is a width, not an
    area, and divided that by the page area to get a "ratio" with the
    wrong units. It happened to be monotonic so nothing looked obviously
    wrong, but the numbers it produced were meaningless.
    """
    rects = [(b[0], b[1], b[2], b[3]) for b in boxes if b[2] > b[0] and b[3] > b[1]]
    if not rects:
        return 0.0
    if len(rects) == 1:
        x0, y0, x1, y1 = rects[0]
        return (x1 - x0) * (y1 - y0)

    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    ys = sorted({r[1] for r in rects} | {r[3] for r in rects})

    total = 0.0
    for i in range(len(xs) - 1):
        cx = (xs[i] + xs[i + 1]) / 2
        for j in range(len(ys) - 1):
            cy = (ys[j] + ys[j + 1]) / 2
            for x0, y0, x1, y1 in rects:
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    total += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
                    break
    return total


def analyse(path: Path | str, max_pages: int = 0) -> DocumentAnalysis:
    """Classify every page of a PDF without rendering anything.

    *max_pages* bounds the work on a pathological document; 0 means all
    of them, which is what a source should use.
    """
    doc_path = Path(path)
    result = DocumentAnalysis(path=doc_path)
    try:
        document = pdfium.PdfDocument(str(doc_path))
    except Exception as exc:  # noqa: BLE001 - a bad PDF is not a crash
        # Worth saying plainly: several sources in this library carry a
        # .pdf extension over an ASCII file or a DEC virtual-paper
        # document, so "no pages" is usually a mislabelled file rather
        # than an empty one.
        result.error = f"not a readable PDF ({str(exc)[:80]})"
        return result

    try:
        limit = len(document) if max_pages <= 0 else min(max_pages, len(document))
        for index in range(limit):
            page = document[index]
            try:
                result.pages.append(_analyse_page(page, index))
            except Exception as exc:  # noqa: BLE001 - one bad page is not fatal
                result.pages.append(
                    PageAnalysis(
                        number=index,
                        kind=PageKind.TEXT,
                        text_chars=0,
                        image_area_ratio=0.0,
                    )
                )
                logger.debug(f"page {index + 1} analysis failed: {exc}")
    finally:
        try:
            document.close()
        except Exception:
            pass
    return result
