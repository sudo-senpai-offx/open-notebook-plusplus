"""Selective OCR for sources that need it.

See `analyze` for how pages are classified and `engine` for how the
recogniser is invoked. The split is deliberate: deciding *what* to read
is pure logic over the PDF's own metadata and is testable without an
engine installed; running the engine is the expensive, platform-specific
half.
"""

from open_notebook.ocr.analyze import (
    GRAPHICAL_AREA_RATIO,
    MIN_REGION_AREA_RATIO,
    MIN_TEXT_CHARS,
    DocumentAnalysis,
    PageAnalysis,
    PageKind,
    analyse,
)
from open_notebook.ocr.engine import (
    OCRResult,
    available,
    ocr_document,
    ocr_dpi,
    ocr_enabled,
    ocr_languages,
    probe,
)

__all__ = [
    "DocumentAnalysis",
    "GRAPHICAL_AREA_RATIO",
    "MIN_REGION_AREA_RATIO",
    "MIN_TEXT_CHARS",
    "OCRResult",
    "PageAnalysis",
    "PageKind",
    "analyse",
    "available",
    "ocr_dpi",
    "ocr_document",
    "ocr_enabled",
    "ocr_languages",
    "probe",
]
