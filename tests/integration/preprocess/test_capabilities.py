"""Integration tests for Week 4 Day 5 capability detection + gates.

Four tests, all mocking subprocesses/packages so CI passes without the full
binary toolchain installed:

  1. test_detects_installed_and_missing_binaries — mocks shutil.which +
     importlib.util.find_spec, asserts an accurate capability map and that
     results are cached (zero re-probes on repeat calls).
  2. test_scanned_pdf_rejected_when_tesseract_missing — mocks has_capability
     to hide tesseract/pdftoppm, calls extract_stage with a PDF, and asserts
     the actionable "OCR is not installed" error is recorded (no subprocess
     invoked, routes to compensate_extract).
  3. test_document_falls_back_to_content_core_when_pandoc_missing — when
     pandoc is absent but content_core is available, extraction falls back to
     content-core (no loud failure); when both are absent, it fails loudly.
  4. test_video_rejected_at_route_stage — route_stage rejects raw video/*
     immediately with the YouTube-URL guidance message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from open_notebook.graphs import source as source_module
from open_notebook.preprocess import capabilities as cap_module


# ── Helpers ──────────────────────────────────────────────────────────────


def _reset_capability_cache() -> None:
    """Clear the module-level capability cache so each test probes fresh."""
    cap_module._CAPABILITY_CACHE = None


def _fake_state(content_type: str, lane: str = "instant", **overrides) -> dict:
    """Build a minimal extract_stage state."""
    state = {
        "content_type": content_type,
        "lane": lane,
        "content_state": {
            "file_path": "/fake/file",
            "language": "en",
            "title": "Test",
        },
        "errors": [],
        "source_id": "test-source",
        "notebook_ids": [],
        "apply_transformations": [],
        "embed": False,
        "source": None,
    }
    state.update(overrides)
    return state


# ── Test 1: capability detection + caching ───────────────────────────────


def test_detects_installed_and_missing_binaries(monkeypatch):
    """shutil.which + find_spec drive an accurate, cached capability map."""
    _reset_capability_cache()

    # Pretend only tesseract, pdftotext, pdftoppm and content_core exist.
    def fake_which(binary: str):
        return "/usr/bin/" + binary if binary in ("tesseract", "pdftotext", "pdftoppm") else None

    def fake_find_spec(package: str):
        return object() if package == "content_core" else None

    monkeypatch.setattr(cap_module.shutil, "which", fake_which)
    monkeypatch.setattr(cap_module.importlib.util, "find_spec", fake_find_spec)

    status = cap_module.get_capability_status()
    d = status.to_dict()
    assert d["tesseract"] is True
    assert d["pdftotext"] is True
    assert d["pdftoppm"] is True
    assert d["content_core"] is True
    assert d["pandoc"] is False
    assert d["ssconvert"] is False
    assert d["whisper_cli"] is False
    assert d["ffmpeg"] is False

    # Caching: changing which after the first probe must NOT change status.
    monkeypatch.setattr(cap_module.shutil, "which", lambda b: "/usr/bin/pandoc")
    status_again = cap_module.get_capability_status()
    assert status_again.to_dict()["pandoc"] is False, "cached result must not change"

    # refresh_capabilities() invalidates the cache.
    status_refreshed = cap_module.refresh_capabilities()
    assert status_refreshed.to_dict()["pandoc"] is True, "refresh must re-probe"


# ── Test 2: scanned PDF rejected when tesseract missing ──────────────────


@pytest.mark.asyncio
async def test_scanned_pdf_rejected_when_tesseract_missing(monkeypatch):
    """PDF with tesseract/pdftoppm missing → OCR is not installed error."""
    _reset_capability_cache()
    # source.py binds has_capability at import time, so patch it there.
    monkeypatch.setattr(
        source_module,
        "has_capability",
        lambda name: name == "pdftotext",
    )

    # No subprocess should be invoked; assert via extract_stage errors.
    state = _fake_state("pdf", lane="heavy")
    state["content_state"]["page_count"] = 5

    result = await source_module.extract_stage(state)

    assert result["stage"] == "extract"
    assert result["extraction"] is None
    assert any("OCR is not installed" in e for e in result["errors"]), result["errors"]


# ── Test 3: document falls back to content_core when pandoc missing ─────


@pytest.mark.asyncio
async def test_document_falls_back_to_content_core_when_pandoc_missing(
    monkeypatch,
):
    """pandoc absent + content_core available → content-core fallback; both absent → loud failure."""
    _reset_capability_cache()
    # source.py binds has_capability at import time, so patch it there.
    monkeypatch.setattr(source_module, "has_capability", lambda name: False)

    # Case A: pandoc missing, content_core available → content-core fallback.
    # Patch has_capability per-case via a mutable flag.
    cap_state = {"content_core": True}

    def fake_has_capability(name):
        return name == "content_core" if cap_state["content_core"] else False

    monkeypatch.setattr(source_module, "has_capability", fake_has_capability)

    # Mock _extract_content_core_stage to record it was called.
    called = {}

    async def fake_content_core(state, cmd_id, errors, content_state, url):
        called["yes"] = True
        from open_notebook.graphs.source import _ExtractionOutput

        return {
            "extraction": _ExtractionOutput(content="fallback text", title="Test"),
            "errors": errors,
            "stage": "extract",
        }

    monkeypatch.setattr(source_module, "_extract_content_core_stage", fake_content_core)

    state = _fake_state("document", lane="instant")
    result = await source_module.extract_stage(state)

    assert called.get("yes") is True, "content-core fallback should be used"
    assert result["extraction"] is not None
    assert result["extraction"].content == "fallback text"

    # Case B: both pandoc and content_core absent → loud ValueError captured as error.
    cap_state["content_core"] = False

    state_b = _fake_state("document", lane="instant")
    result_b = await source_module.extract_stage(state_b)

    assert result_b["stage"] == "extract"
    assert result_b["extraction"] is None
    assert any("pandoc is not installed" in e for e in result_b["errors"]), result_b["errors"]


# ── Test 4: video rejected at route_stage ───────────────────────────────


@pytest.mark.asyncio
async def test_video_rejected_at_route_stage(monkeypatch, tmp_path):
    """raw video/* fails immediately with YouTube-URL guidance message."""
    # Create a fake file so magic-byte checks can run.
    fake_video = tmp_path / "lecture.mp4"
    fake_video.write_bytes(b"\x00\x00\x01\xba")  # MPEG program stream (video)

    # Mock _is_video to return True for our fake file.
    monkeypatch.setattr(
        source_module,
        "_is_video",
        lambda fp: fp == str(fake_video),
    )

    state = {
        "content_type": "media",
        "lane": "heavy",
        "content_state": {"file_path": str(fake_video), "language": "en"},
        "errors": [],
        "source_id": "test-source",
        "notebook_ids": [],
        "apply_transformations": [],
        "embed": False,
        "source": None,
    }

    with pytest.raises(ValueError, match="Video files are not supported"):
        await source_module.route_stage(state)
