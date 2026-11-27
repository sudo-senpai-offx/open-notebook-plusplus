"""Integration tests for Week 5 Day 2: whisper.cpp STT wiring + capability gating.

Tests mock subprocesses/packages so CI passes without whisper-cli/ffmpeg
installed:

  1. test_stt_model_path_resolution — settings → OPEN_NOTEBOOK_WHISPER_MODEL
     → ~/.onpp/models/ggml-base-q5_1.bin; missing model raises ValueError.
  2. test_stt_short_audio_requires_explicit_language — < 60s clip without
     source.language raises ValueError("LANGUAGE_REQUIRED_FOR_SHORT_AUDIO").
  3. test_audio_route_gates_whisper_and_ffmpeg — route_stage classifies audio
     as content_type="audio", heavy lane; missing whisper-cli/ffmpeg fails
     loudly via require_capability.
  4. test_extract_stt_stage_persists_detected_language — auto-detected
     language is written back to source.language with a warning.
"""

from __future__ import annotations

import pytest

from open_notebook.graphs import source as source_module
from open_notebook.preprocess import capabilities as cap_module
from open_notebook.preprocess.tools import stt as stt_module
from open_notebook.preprocess.tools.stt import (
    LANGUAGE_REQUIRED_FOR_SHORT_AUDIO,
    resolve_model_path,
    transcribe_audio,
)
from open_notebook.preprocess.capabilities import CapabilityStatus


# ── Helpers ──────────────────────────────────────────────────────────────


def _set_capabilities(**fields) -> None:
    """Populate the capability cache so require_capability/has_capability
    return the given values (source.py binds these at import time)."""
    cap_module._CAPABILITY_CACHE = CapabilityStatus(**fields)


def _reset_capability_cache() -> None:
    cap_module._CAPABILITY_CACHE = None


def _fake_state(content_type: str, lane: str = "heavy", **overrides) -> dict:
    state = {
        "content_type": content_type,
        "lane": lane,
        "content_state": {
            "file_path": "/fake/audio.mp3",
            "language": "en",
            "title": "Test Audio",
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


# ── Test 1: model path resolution ───────────────────────────────────────


def test_stt_model_path_resolution(monkeypatch):
    """settings → OPEN_NOTEBOOK_WHISPER_MODEL → default; missing → ValueError."""
    monkeypatch.delenv("OPEN_NOTEBOOK_WHISPER_MODEL", raising=False)

    # Default missing → ValueError.
    with pytest.raises(ValueError, match="Whisper model not found"):
        resolve_model_path()

    # Explicit setting that exists.
    assert resolve_model_path("/usr/bin/ffmpeg") == "/usr/bin/ffmpeg"

    # Explicit setting missing → ValueError.
    with pytest.raises(ValueError, match="Whisper model not found"):
        resolve_model_path("/nonexistent/model.bin")

    # Env var that exists.
    monkeypatch.setenv("OPEN_NOTEBOOK_WHISPER_MODEL", "/usr/bin/ffmpeg")
    assert resolve_model_path() == "/usr/bin/ffmpeg"


# ── Test 2: short audio requires explicit language ──────────────────────


@pytest.mark.asyncio
async def test_stt_short_audio_requires_explicit_language(tmp_path, monkeypatch):
    """< 60s clip without source.language raises ValueError."""
    fake_audio = tmp_path / "short.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x64")  # MPEG frame sync (MP3)

    monkeypatch.setattr(stt_module, "_probe_duration", lambda fp: 30.0)

    with pytest.raises(ValueError, match=LANGUAGE_REQUIRED_FOR_SHORT_AUDIO):
        await transcribe_audio(
            command_id="cmd",
            file_path=str(fake_audio),
            language=None,
            source_language_set=False,
        )


# ── Test 3: audio route gates whisper + ffmpeg ──────────────────────────


@pytest.mark.asyncio
async def test_audio_route_gates_whisper_and_ffmpeg(tmp_path, monkeypatch):
    """audio/* → content_type='audio', heavy lane; missing binary → loud error."""
    fake_audio = tmp_path / "song.wav"
    fake_audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")  # WAV

    class FakeSource:
        language = None

        async def save(self):
            pass

    _reset_capability_cache()

    # Case A: whisper-cli + ffmpeg present → route succeeds.
    _set_capabilities(whisper_cli=True, ffmpeg=True, tesseract=True, pdftotext=True, pdftoppm=True, ssconvert=True, content_core=True)

    state = _fake_state("audio", lane="heavy")
    state["content_state"]["file_path"] = str(fake_audio)
    state["source"] = FakeSource()

    monkeypatch.setattr(
        source_module, "_audio_duration_seconds", lambda fp: 300.0
    )

    route_result = await source_module.route_stage(state)
    assert route_result["content_type"] == "audio"
    assert route_result["lane"] == "heavy"
    assert "audio file detected" in route_result["route_reason"]

    # Case B: whisper-cli missing → extract_stage fails loudly.
    _set_capabilities(whisper_cli=False, ffmpeg=True, tesseract=True, pdftotext=True, pdftoppm=True, ssconvert=True, content_core=True)

    state_b = _fake_state("audio", lane="heavy")
    state_b["content_state"]["file_path"] = str(fake_audio)
    state_b["source"] = FakeSource()

    extract_result = await source_module.extract_stage(state_b)
    assert extract_result["stage"] == "extract"
    assert extract_result["extraction"] is None
    assert any(
        "whisper-cli" in e and "is not installed" in e
        for e in extract_result["errors"]
    ), extract_result["errors"]


# ── Test 4: extract STT persists detected language ──────────────────────


@pytest.mark.asyncio
async def test_extract_stt_stage_persists_detected_language(
    tmp_path, monkeypatch
):
    """Auto-detected language written back to source.language + warning."""
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"\xff\xfb\x90\x64")

    class FakeSource:
        language = None

        saved = {}

        async def save(self):
            FakeSource.saved["language"] = self.language

    source = FakeSource()

    _reset_capability_cache()
    _set_capabilities(
        whisper_cli=True, ffmpeg=True, tesseract=True, pdftotext=True,
        pdftoppm=True, ssconvert=True, content_core=True,
    )
    monkeypatch.setattr(
        source_module, "_audio_duration_seconds", lambda fp: 300.0
    )

    # Model resolves to a real file so resolve_model_path doesn't raise.
    monkeypatch.setattr(
        source_module, "resolve_model_path", lambda setting=None: "/usr/bin/ffmpeg"
    )

    async def fake_transcribe_audio(command_id, file_path, language, model_path, source_language_set):
        return {
            "text": "hello world transcript",
            "segments_processed": 1,
            "segments_total": 1,
            "timed_out_segments": [],
            "checkpoint_position": 1,
            "detected_language": "es",
            "warnings": [],
            "source_status": None,
        }

    monkeypatch.setattr(source_module, "transcribe_audio", fake_transcribe_audio)

    state = _fake_state("audio", lane="heavy")
    state["content_state"]["file_path"] = str(fake_audio)
    state["source"] = source

    result = await source_module.extract_stage(state)

    assert result["stage"] == "extract"
    assert result["extraction"] is not None
    assert result["extraction"].content == "hello world transcript"
    # Detected language persisted to source.
    assert source.language == "es"
    assert FakeSource.saved.get("language") == "es"
    # Warning attached.
    assert any("Whisper detected es" in e for e in result["errors"]), result["errors"]
