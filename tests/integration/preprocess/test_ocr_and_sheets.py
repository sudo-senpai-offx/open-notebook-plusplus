"""Integration tests for Week 4 Day 4 preprocess tools (ssconvert + tesseract).

Four tests, all mocking subprocesses so CI passes without tesseract/ssconvert
binaries installed:

  1. test_ssconvert_converts_xlsx_to_markdown — multi-row CSV export converts
     cleanly to a Markdown table.
  2. test_tesseract_per_range_checkpoint_and_timeout — 5-page range
     checkpointing, OMP_THREAD_LIMIT=1 env, and extracting_partial on range
     timeout.
  3. test_tesseract_flags_low_confidence — mocks TSV output with < 60%
     confidence, asserts "low_ocr_confidence" warning is attached.
  4. test_extract_stage_uses_1800s_timeout_for_heavy_ocr — verifies heavy OCR
     route gets the 1800s stage timeout instead of 300s.

Brings test count from 60 → 64 (Day 3 had 60 target; Day 4 adds 4).
"""

from __future__ import annotations

import asyncio
import os
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.graphs import source as source_module
from open_notebook.preprocess.tools.ssconvert import (
    convert_spreadsheet,
    csv_to_markdown_table,
    is_spreadsheet,
)
from open_notebook.preprocess.tools.tesseract import (
    DEFAULT_LANGUAGE,
    extract_ocr,
    _build_tesseract_env,
    _mean_confidence,
    _ocr_single_image,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_xlsx(tmp_path, sheet_name: str = "Sheet1") -> str:
    """Create a minimal valid XLSX (zip with sheet XML) for ssconvert testing.

    Note: ssconvert isn't installed in CI, so convert_spreadsheet is tested
    with a mocked subprocess. This fixture just needs to be a path that
    exists and ends in .xlsx.
    """
    xlsx_path = os.path.join(tmp_path, "test.xlsx")
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Sheet1" sheetId="1" state="hidden" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    return xlsx_path


def _fake_subprocess(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Return an AsyncMock subprocess with the given communicate result."""
    mock_proc = AsyncMock()
    mock_proc.returncode = returncode
    mock_proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    return mock_proc


# ── Test 1: ssconvert converts XLSX to Markdown table ───────────────────


def test_ssconvert_converts_xlsx_to_markdown(tmp_path):
    """csv_to_markdown_table turns multi-row CSV into a Markdown table."""
    csv_text = "Name,Age,City\nAlice,30,New York\nBob,25,London\nCarol,35,Tokyo\n"
    markdown, truncated = csv_to_markdown_table(csv_text)

    assert not truncated
    assert "| Name | Age | City |" in markdown
    assert "| --- | --- | --- |" in markdown
    assert "| Alice | 30 | New York |" in markdown
    assert "| Bob | 25 | London |" in markdown
    assert "| Carol | 35 | Tokyo |" in markdown


@pytest.mark.asyncio
async def test_ssconvert_convert_spreadsheet_mocks_subprocess(
    tmp_path, monkeypatch
):
    """convert_spreadsheet calls ssconvert subprocess and returns Markdown."""
    xlsx_path = _make_xlsx(tmp_path)
    csv_output = "Name,Age\nAlice,30\nBob,25\n"
    fake_proc = _fake_subprocess(returncode=0, stdout="", stderr="")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.ssconvert.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    # Patch open to return the CSV output instead of the real temp file.
    real_open = open

    def fake_open(path, mode="r", *a, **k):
        if path.endswith(".csv") and "w" in mode:
            # This is the output temp file we write to; capture it.
            return real_open(path, mode, *a, **k)
        if path.endswith(".csv") and "r" in mode:
            # This is reading the output CSV — return our fixture.
            import io

            return io.StringIO(csv_output)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)

    result = await convert_spreadsheet(command_id="test-cmd", file_path=xlsx_path)

    assert "| Name | Age |" in result["text"]
    assert "| Alice | 30 |" in result["text"]
    assert "| Bob | 25 |" in result["text"]
    assert result["pages_processed"] == 1
    assert result["checkpoint_position"] == 1


# ── Test 2: tesseract per-range checkpoint + timeout ────────────────────


@pytest.mark.asyncio
async def test_tesseract_per_range_checkpoint_and_timeout(tmp_path, monkeypatch):
    """5-page range checkpointing + OMP_THREAD_LIMIT=1 + timeout recovery."""
    # Create a fake file so os.path.isfile passes.
    fake_pdf = tmp_path / "scanned.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    # Mock _ocr_single_image to succeed for first range, timeout for second.
    call_count = {"n": 0}

    async def fake_ocr_single(image_path, language, page_num, tmp_dir):
        call_count["n"] += 1
        if call_count["n"] <= 5:  # first 5 pages (1 range)
            return "Page text " + str(page_num), True, 85.0
        # Second range: simulate timeout by raising asyncio.TimeoutError
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract._ocr_single_image",
        fake_ocr_single,
    )

    # Mock _rasterize_page_range to return 5 fake PNG paths per range.
    async def fake_rasterize(file_path, start_page, end_page, dpi, tmp_dir):
        return [
            os.path.join(tmp_dir, f"page-{start_page + i:04d}-1.png")
            for i in range(end_page - start_page + 1)
        ]

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract._rasterize_page_range",
        fake_rasterize,
    )

    # Mock JobCheckpoint to capture checkpoint writes.
    captured = {}

    class FakeJobCheckpoint:
        def __init__(self, command_id=None, checkpoint=None, **kwargs):
            self.command_id = command_id
            self.checkpoint = checkpoint or {}

        @classmethod
        async def get_for_command(cls, command_id):
            return None

        @classmethod
        async def upsert_for_command(cls, command_id, checkpoint=None, increment_attempt=False):
            captured[command_id] = checkpoint
            return cls(command_id, checkpoint=checkpoint or {})

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract.JobCheckpoint",
        FakeJobCheckpoint,
    )

    result = await extract_ocr(
        command_id="test-cmd",
        file_path=str(fake_pdf),
        page_count=10,
        language="eng",
    )

    # First range (pages 1-5) succeeded, second range (6-10) timed out.
    assert result["pages_processed"] == 5
    assert result["timed_out_pages"] == [6, 7, 8, 9, 10]
    # Checkpoint advances to end of last attempted range (consistent with pdf.py).
    assert result["checkpoint_position"] == 10
    assert "test-cmd" in captured
    assert captured["test-cmd"]["pages_processed"] == 5

    # Verify OMP_THREAD_LIMIT=1 is set in tesseract env.
    env = _build_tesseract_env()
    assert env["OMP_THREAD_LIMIT"] == "1"


# ── Test 3: tesseract flags low confidence ──────────────────────────────


@pytest.mark.asyncio
async def test_tesseract_flags_low_confidence(tmp_path, monkeypatch):
    """TSV output with < 60% mean confidence → low_ocr_confidence warning."""
    # Create a fake file so os.path.isfile passes.
    fake_pdf = tmp_path / "scanned.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    # TSV with low confidences (all words below 60%).
    tsv_lines = [
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
        "5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t30\tlowconf",   # conf=30
        "5\t1\t1\t1\t1\t2\t35\t10\t20\t10\t40\tword2",      # conf=40
        "5\t1\t1\t1\t1\t3\t60\t10\t20\t10\t50\tword3",      # conf=50
    ]

    async def fake_ocr_single(image_path, language, page_num, tmp_dir):
        # Write a fake TSV file for confidence parsing.
        tsv_path = os.path.join(tmp_dir, f"ocr-{page_num}.tsv")
        with open(tsv_path, "w") as f:
            f.write("\n".join(tsv_lines))
        # Write fake OCR text output.
        txt_path = os.path.join(tmp_dir, f"ocr-{page_num}.txt")
        with open(txt_path, "w") as f:
            f.write("OCR text output\n")
        return "OCR text output", True, _mean_confidence("\n".join(tsv_lines))

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract._ocr_single_image",
        fake_ocr_single,
    )

    async def fake_rasterize(file_path, start_page, end_page, dpi, tmp_dir):
        return [os.path.join(tmp_dir, "page-0001-1.png")]

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract._rasterize_page_range",
        fake_rasterize,
    )

    captured = {}

    class FakeJobCheckpoint:
        def __init__(self, command_id=None, checkpoint=None, **kwargs):
            self.command_id = command_id
            self.checkpoint = checkpoint or {}

        @classmethod
        async def get_for_command(cls, command_id):
            return None

        @classmethod
        async def upsert_for_command(cls, command_id, checkpoint=None, increment_attempt=False):
            captured[command_id] = checkpoint
            return cls(command_id, checkpoint=checkpoint or {})

    monkeypatch.setattr(
        "open_notebook.preprocess.tools.tesseract.JobCheckpoint",
        FakeJobCheckpoint,
    )

    result = await extract_ocr(
        command_id="test-cmd",
        file_path=str(fake_pdf),
        page_count=1,
        language="eng",
    )

    # Mean confidence = (30+40+50)/3 = 40% < 60% → low_confidence flagged.
    assert result["low_confidence"] is True
    assert any("low_ocr_confidence" in w for w in result["warnings"])
    assert any("40.0%" in w or "40%" in w for w in result["warnings"])


# ── Test 4: extract_stage uses 1800s timeout for heavy OCR ─────────────


@pytest.mark.asyncio
async def test_extract_stage_uses_1800s_timeout_for_heavy_ocr(monkeypatch):
    """Heavy lane (image/PDF) gets 1800s stage timeout, instant gets 300s."""
    # Verify the module-level constant.
    assert source_module.STAGE_TIMEOUT_HEAVY == 1800.0
    assert source_module.STAGE_TIMEOUT_EXTRACT == 300.0

    # Build a heavy-lane state (image content_type).
    state = {
        "content_type": "image",
        "lane": "heavy",
        "content_state": {
            "file_path": "/fake/image.png",
            "language": "eng",
            "title": "Test Image",
        },
        "errors": [],
        "source_id": "test-source",
        "notebook_ids": [],
        "apply_transformations": [],
        "embed": False,
        "source": None,
    }

    # Mock extract_ocr to return a successful result.
    async def fake_extract_ocr(command_id, file_path, page_count, language, dpi=300):
        return {
            "text": "OCR extracted text",
            "pages_processed": 1,
            "pages_total": 1,
            "timed_out_pages": [],
            "checkpoint_position": 1,
            "low_confidence": False,
            "warnings": [],
        }

    monkeypatch.setattr(
        source_module, "extract_ocr", fake_extract_ocr
    )

    result = await source_module.extract_stage(state)

    assert result["extraction"] is not None
    assert result["extraction"].content == "OCR extracted text"
    assert result["stage"] == "extract"
    assert len(result["errors"]) == 0


@pytest.mark.asyncio
async def test_extract_stage_uses_300s_timeout_for_instant(monkeypatch):
    """Instant lane (text/url) gets 300s stage timeout."""
    state = {
        "content_type": "text",
        "lane": "instant",
        "content_state": {"content": "Direct text content here"},
        "errors": [],
        "source_id": "test-source",
        "notebook_ids": [],
        "apply_transformations": [],
        "embed": False,
        "source": None,
    }

    result = await source_module.extract_stage(state)

    assert result["extraction"] is not None
    assert "Direct text content" in result["extraction"].content
    assert result["stage"] == "extract"


# ── is_spreadsheet / is_csv sanity ──────────────────────────────────────


def test_is_spreadsheet_and_is_csv():
    """Extension detectors return correct results."""
    assert is_spreadsheet("/path/to/file.xlsx")
    assert is_spreadsheet("/path/to/file.xls")
    assert is_spreadsheet("/path/to/file.ods")
    assert not is_spreadsheet("/path/to/file.pdf")
    assert not is_spreadsheet("/path/to/file.csv")

    from open_notebook.preprocess.tools.ssconvert import is_csv

    assert is_csv("/path/to/file.csv")
    assert not is_csv("/path/to/file.xlsx")
    assert not is_csv("/path/to/file.pdf")
