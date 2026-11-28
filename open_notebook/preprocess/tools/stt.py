"""whisper.cpp STT wrapper for Week 5 poly-pipeline.

Week 5 Day 1: Speech-to-Text via whisper.cpp CLI (Heavy / Deferred Lane).

Replaces the legacy HTTP stt_url() / silent _try_stt path (Audit Q8 &
Failure #1) with direct CLI invocation and chunked checkpointing, mirroring
the pdf.py / tesseract.py per-segment timeout + intra-stage JobCheckpoint
pattern.

Pipeline:
  1. Probe audio duration via ffprobe.
  2. Enforce the Week 1 Q3 rule: audio < 60s without an explicit
     source.language raises ValueError("LANGUAGE_REQUIRED_FOR_SHORT_AUDIO").
  3. Convert non-WAV audio (.mp3, .m4a, .ogg, .flac, .webm) to 16 kHz mono
     WAV via ffmpeg in a temp directory (cleaned up on exit).
  4. Split audio > 10 min into 5-minute segments; run whisper-cli
     --model <model_path> --language <lang|auto> --output-txt per segment
     with a 180s per-segment timeout.
  5. Write an intra-stage JobCheckpoint after each segment so a kill -9
     mid-transcription preserves completed segments.
  6. On segment timeout: record timed_out_segments, append
     "stt_partial_timeout" warning, flag extracting_partial, continue.
  7. Auto-detect feedback: when source.language is unset (--language auto),
     parse the detected language from whisper-cli output, persist it back
     to source.language, and attach a "Whisper detected <lang>..." warning.

Returns an ExtractResult dict compatible with extract_stage:

    {
        "text": str,                    # concatenated transcript
        "segments_processed": int,      # segments successfully transcribed
        "segments_total": int,          # total segments attempted
        "timed_out_segments": List[int],# segment indices that timed out
        "checkpoint_position": int,     # last checkpointed segment index
        "detected_language": Optional[str],  # auto-detected language (if any)
        "warnings": List[str],          # human-readable warnings
        "source_status": Optional[str], # "extracting_partial" if partial
    }
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

# Binary names resolved at call time so tests can monkeypatch them.
WHISPER_BINARY = "whisper-cli"
FFMPEG_BINARY = "ffmpeg"
FFPROBE_BINARY = "ffprobe"

# Segmenting: split audio > 10 minutes into 5-minute segments.
SEGMENT_SECONDS = 300          # 5 minutes per segment
LONG_AUDIO_SECONDS = 600       # chunking threshold: 10 minutes
SHORT_AUDIO_SECONDS = 60       # below this, an explicit language is required
SEGMENT_TIMEOUT_SECONDS = 180  # 3 minutes per whisper-cli invocation

# Audio normalization target (16 kHz mono WAV).
SAMPLE_RATE = 16000
CHANNELS = 1

# Whisper model resolution: explicit settings → env var → RAM-aware default → on-disk path.
WHISPER_MODEL_ENV = "OPEN_NOTEBOOK_WHISPER_MODEL"
DEFAULT_MODEL_DIR = os.path.expanduser("~/.onpp/models")
# Quantized base model (q5_1) specified in Week 1 Q8 for both en and multilingual.
DEFAULT_MODEL_NAME = "ggml-base-q5_1.bin"
# RAM-tiered model filenames (all q5_1 quantized).
WHISPER_TINY_MODEL = "ggml-tiny-q5_1.bin"      # ~32 MB disk, ~500 MB RAM
WHISPER_BASE_MODEL = "ggml-base-q5_1.bin"      # ~60 MB disk, ~1 GB RAM
WHISPER_SMALL_MODEL = "ggml-small-q5_1.bin"    # ~190 MB disk, ~2 GB RAM

# RAM thresholds (MB) for model tier selection.
RAM_TINY_MAX_MB = 2048       # < 2 GB: tiny
RAM_BASE_MAX_MB = 4096       # 2-4 GB: base
# > 4 GB: small

# Week 1 Q3 rule message.
LANGUAGE_REQUIRED_FOR_SHORT_AUDIO = "LANGUAGE_REQUIRED_FOR_SHORT_AUDIO"

# Default language when unset: whisper-cli "auto" triggers detection.
AUTO_LANGUAGE = "auto"

# Regex to parse the detected language from whisper-cli output.
# whisper.cpp prints lines like "Detected language: en" or similar.
_DETECTED_LANG_RE = re.compile(
    r"detected language[:\s]+([a-z]{2}(?:-[A-Z]{2})?)", re.IGNORECASE
)


def _detect_available_ram_mb() -> Optional[int]:
    """Detect available system RAM in MB.

    Reuses the live ``MemAvailable`` signal from
    ``open_notebook.utils.capability_tier.mem_available_mb`` so the
    STT model tier and the training tier gate stay consistent.
    Returns None when memory cannot be determined (fails safe → base).
    """
    try:
        from open_notebook.utils.capability_tier import mem_available_mb

        return mem_available_mb()
    except Exception:  # noqa: BLE001 - import failure must not break STT
        return None


def _select_whisper_model_by_ram(available_ram_mb: Optional[int]) -> str:
    """Select a whisper.cpp model filename based on available RAM.

    Selection tiers:
      - RAM unknown or < 2048 MB: tiny  (ggml-tiny-q5_1.bin)
      - 2048-4096 MB:               base (ggml-base-q5_1.bin)
      - > 4096 MB:                  small (ggml-small-q5_1.bin)

    Unknown RAM fails safe to the base model — the safest default that
    works on both 2 GB and 4 GB devices without OOMing.
    """
    if available_ram_mb is None:
        return WHISPER_BASE_MODEL
    if available_ram_mb < RAM_TINY_MAX_MB:
        return WHISPER_TINY_MODEL
    if available_ram_mb < RAM_BASE_MAX_MB:
        return WHISPER_BASE_MODEL
    return WHISPER_SMALL_MODEL


def _model_path_for_name(model_name: str) -> str:
    """Return the on-disk path for a whisper model name."""
    return os.path.join(DEFAULT_MODEL_DIR, model_name)


def resolve_model_path(stt_model_setting: Optional[str] = None) -> str:
    """Resolve the whisper.cpp model path.

    Resolution order (first existing file wins):
      1. ``stt_model_setting`` (persisted ContentSettings.stt_model)
      2. ``OPEN_NOTEBOOK_WHISPER_MODEL`` environment variable
      3. RAM-aware default model at ``~/.onpp/models/<tier_model>``
      4. Fallback chain: base → tiny → small (whichever exists on disk)
      5. Hardcoded default path ``~/.onpp/models/ggml-base-q5_1.bin``

    Raises:
        ValueError: if an explicitly-configured path does not exist, or if
            no usable model is found anywhere in the resolution chain.
    """
    # 1. Explicitly-configured setting — must exist if set.
    if stt_model_setting:
        if os.path.isfile(stt_model_setting):
            return stt_model_setting
        raise ValueError(
            f"Whisper model not found at {stt_model_setting}. "
            f"Download {DEFAULT_MODEL_NAME} or set {WHISPER_MODEL_ENV}."
        )

    # 2. Environment variable — must exist if set.
    env_path = os.getenv(WHISPER_MODEL_ENV)
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        raise ValueError(
            f"Whisper model not found at {env_path}. "
            f"Download {DEFAULT_MODEL_NAME} or unset {WHISPER_MODEL_ENV}."
        )

    # 3. RAM-aware default model selection.
    ram_mb = _detect_available_ram_mb()
    selected_name = _select_whisper_model_by_ram(ram_mb)
    ram_label = f"{ram_mb} MB" if ram_mb is not None else "unknown"
    logger.debug(
        f"Whisper RAM-aware model selection: {ram_label} RAM → {selected_name}"
    )

    # Build a preference chain: selected model first, then fallbacks.
    preference_order = [selected_name]
    for fallback in (WHISPER_BASE_MODEL, WHISPER_TINY_MODEL, WHISPER_SMALL_MODEL):
        if fallback not in preference_order:
            preference_order.append(fallback)

    for model_name in preference_order:
        candidate = _model_path_for_name(model_name)
        if os.path.isfile(candidate):
            return candidate

    # 4. Nothing on disk — raise with actionable message.
    raise ValueError(
        f"Whisper model not found at {_model_path_for_name(DEFAULT_MODEL_NAME)}. "
        f"Download {DEFAULT_MODEL_NAME} (or a RAM-appropriate tier model) "
        f"or set {WHISPER_MODEL_ENV}."
    )


@dataclass
class ExtractResult:
    """Result of STT transcription."""

    text: str = ""
    segments_processed: int = 0
    segments_total: int = 0
    timed_out_segments: List[int] = field(default_factory=list)
    checkpoint_position: int = 0
    detected_language: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    source_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "segments_processed": self.segments_processed,
            "segments_total": self.segments_total,
            "timed_out_segments": self.timed_out_segments,
            "checkpoint_position": self.checkpoint_position,
            "detected_language": self.detected_language,
            "warnings": self.warnings,
            "source_status": self.source_status,
        }


async def _write_checkpoint(
    command_id: str,
    position: int,
    segments_processed: int,
    timed_out_segments: List[int],
    text_accumulator: List[str],
    detected_language: Optional[str],
    warnings: List[str],
) -> None:
    """Write an intra-stage checkpoint so resume continues from position."""
    from open_notebook.ai.models import JobCheckpoint

    checkpoint_data = {
        "stage": "extract",
        "position": position,
        "segments_processed": segments_processed,
        "timed_out_segments": timed_out_segments,
        "text_length": sum(len(t) for t in text_accumulator),
        "detected_language": detected_language,
        "warnings": warnings,
    }
    try:
        await JobCheckpoint.upsert_for_command(
            command_id,
            checkpoint=checkpoint_data,
            increment_attempt=False,
        )
    except Exception as exc:  # noqa: BLE001 - checkpoint failure must not stop transcription
        logger.debug(f"checkpoint write failed for {command_id} at {position}: {exc}")


async def _read_checkpoint(command_id: str) -> Optional[Dict[str, Any]]:
    """Read the last checkpoint for resume."""
    from open_notebook.ai.models import JobCheckpoint

    try:
        cp = await JobCheckpoint.get_for_command(command_id)
        if cp is None:
            return None
        return cp.checkpoint or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"checkpoint read failed for {command_id}: {exc}")
        return None


def _whisper_binary() -> str:
    """Resolve the whisper binary to invoke.

    Honors the ``WHISPER_BINARY`` constant (monkeypatchable in tests) when it
    is on PATH; otherwise falls back to the capabilities-probed name
    (``whisper-cli`` → ``whisper``). Falls back to the constant as a last
    resort so callers get a deterministic value even when nothing is installed.
    """
    if shutil.which(WHISPER_BINARY):
        return WHISPER_BINARY
    from open_notebook.preprocess.capabilities import get_whisper_binary_name

    resolved = get_whisper_binary_name()
    return resolved if resolved is not None else WHISPER_BINARY


def _probe_duration(file_path: str) -> Optional[float]:
    """Probe audio duration in seconds via ffprobe. Returns None on failure."""
    try:
        result = subprocess.run(
            [
                FFPROBE_BINARY,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                f"ffprobe returned {result.returncode} for {file_path}: "
                f"{result.stderr.decode(errors='replace')}"
            )
            return None
        return float(result.stdout.decode(errors="replace").strip() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ffprobe failed for {file_path}: {exc}")
        return None


def _normalize_to_wav(file_path: str, tmp_dir: str) -> str:
    """Convert audio to 16 kHz mono WAV via ffmpeg.

    Returns the path to the normalized WAV file. WAV inputs are returned
    as-is (no re-encode needed).
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".wav":
        return file_path

    out_path = os.path.join(tmp_dir, "normalized.wav")
    cmd: List[str] = [
        FFMPEG_BINARY,
        "-i",
        file_path,
        "-vn",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "wav",
        "-y",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=300
        )
        if result.returncode != 0:
            logger.warning(
                f"ffmpeg returned {result.returncode} for {file_path}: "
                f"{result.stderr.decode(errors='replace')}"
            )
            raise RuntimeError(f"ffmpeg normalization failed: {result.stderr.decode(errors='replace')}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ffmpeg normalization failed for {file_path}: {exc}")
        raise
    return out_path


def _cut_segment(
    wav_path: str, start_seconds: float, duration_seconds: float, out_path: str
) -> None:
    """Cut a segment of audio from wav_path into out_path via ffmpeg."""
    cmd: List[str] = [
        FFMPEG_BINARY,
        "-ss",
        str(start_seconds),
        "-i",
        wav_path,
        "-t",
        str(duration_seconds),
        "-c",
        "copy",
        "-y",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=60
        )
        if result.returncode != 0:
            logger.warning(
                f"ffmpeg segment cut returned {result.returncode}: "
                f"{result.stderr.decode(errors='replace')}"
            )
            raise RuntimeError(
                f"ffmpeg segment cut failed: {result.stderr.decode(errors='replace')}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ffmpeg segment cut failed: {exc}")
        raise


def _parse_detected_language(output: str) -> Optional[str]:
    """Parse the detected language from whisper-cli stdout/stderr."""
    match = _DETECTED_LANG_RE.search(output)
    if match:
        return match.group(1).lower()
    return None


async def _transcribe_segment(
    segment_path: str,
    model_path: Optional[str],
    language: Optional[str],
    segment_index: int,
) -> tuple[str, bool, Optional[str]]:
    """Run whisper-cli on a single segment.

    Returns (text, success, detected_language). language=None means
    "--language auto" (whisper auto-detects).

    Applies a 180s per-segment timeout.
    """
    cmd: List[str] = [_whisper_binary()]
    if model_path:
        cmd.extend(["--model", model_path])
    cmd.extend(["--language", language or AUTO_LANGUAGE, "--output-txt", segment_path])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SEGMENT_TIMEOUT_SECONDS
        )
        if proc.returncode != 0:
            logger.warning(
                f"whisper-cli returned {proc.returncode} for segment "
                f"{segment_index}: {stderr.decode(errors='replace')}"
            )
            return "", False, None
    except asyncio.TimeoutError:
        logger.warning(
            f"whisper-cli timed out after {SEGMENT_TIMEOUT_SECONDS}s for "
            f"segment {segment_index}"
        )
        return "", False, None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"whisper-cli failed for segment {segment_index}: {exc}")
        return "", False, None

    combined = (
        stdout.decode(errors="replace")
        + "\n"
        + stderr.decode(errors="replace")
    )
    text = stdout.decode(errors="replace").strip()
    detected_language = _parse_detected_language(combined)
    return text, True, detected_language


async def transcribe_audio(
    command_id: Optional[str],
    file_path: str,
    language: Optional[str] = None,
    model_path: Optional[str] = None,
    source_language_set: bool = False,
) -> Dict[str, Any]:
    """Transcribe an audio file via whisper.cpp CLI.

    Args:
        command_id: The command id for checkpointing and progress reporting.
        file_path: Path to the audio file.
        language: Explicit source language (two-letter code), or None for
            auto-detection.
        model_path: Optional path to a whisper.cpp model file.
        source_language_set: Whether the source already had an explicit
            language declared (used to enforce the short-audio rule).

    Returns:
        ExtractResult dict with text, segments_processed, segments_total,
        timed_out_segments, checkpoint_position, detected_language,
        warnings, and source_status.

    Raises:
        ValueError: if audio < 60s and no explicit language is set
            ("LANGUAGE_REQUIRED_FOR_SHORT_AUDIO"); or if no whisper model
            can be resolved (see resolve_model_path).
        FileNotFoundError: if whisper-cli or ffmpeg is missing.
        asyncio.TimeoutError: if a segment exceeds the per-segment timeout
            (handled internally: recorded and continued).
    """
    if not file_path or not os.path.isfile(file_path):
        return ExtractResult(
            text="",
            segments_processed=0,
            segments_total=0,
            timed_out_segments=[],
            checkpoint_position=0,
        ).to_dict()

    # ── Step 1: probe duration (Week 1 Q3 short-audio rule) ──────────
    duration = _probe_duration(file_path)
    if duration is None:
        duration = 0.0

    if duration < SHORT_AUDIO_SECONDS and not source_language_set:
        raise ValueError(
            f"{LANGUAGE_REQUIRED_FOR_SHORT_AUDIO}: audio is "
            f"{duration:.0f}s (< 60s) and no explicit language was set. "
            f"Set the source's language field before processing."
        )

    # ── Step 1b: resolve + validate the whisper model path ─────────────
    # Fail loudly before spawning whisper-cli if no usable model exists.
    if model_path is None:
        model_path = resolve_model_path()

    # ── Step 2: normalize to 16 kHz mono WAV ─────────────────────────
    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            wav_path = _normalize_to_wav(file_path, tmp_dir)
        except RuntimeError:
            return ExtractResult(
                text="",
                segments_processed=0,
                segments_total=0,
                timed_out_segments=[],
                checkpoint_position=0,
                warnings=["stt_normalization_failed: ffmpeg could not normalize audio"],
            ).to_dict()

        # ── Step 3: chunking decision ────────────────────────────────
        segment_boundaries: List[tuple[float, float]] = []
        if duration > LONG_AUDIO_SECONDS:
            start = 0.0
            while start < duration:
                seg_dur = min(SEGMENT_SECONDS, duration - start)
                segment_boundaries.append((start, seg_dur))
                start += SEGMENT_SECONDS
        else:
            segment_boundaries.append((0.0, duration))

        result = ExtractResult(segments_total=len(segment_boundaries))

        # ── Resume from checkpoint if available ──────────────────────
        resume_from = 0
        if command_id:
            cp = await _read_checkpoint(command_id)
            if cp and cp.get("stage") == "extract":
                resume_from = int(cp.get("position", 0))
                result.segments_processed = int(cp.get("segments_processed", 0))
                result.timed_out_segments = list(cp.get("timed_out_segments", []))
                result.checkpoint_position = resume_from
                result.detected_language = cp.get("detected_language")
                result.warnings = list(cp.get("warnings", []))
                logger.info(
                    f"Resuming STT transcription for {command_id} from "
                    f"segment {resume_from}"
                )

        text_accumulator: List[str] = []

        # ── Step 4: transcribe each segment ──────────────────────────
        for seg_idx in range(resume_from, len(segment_boundaries)):
            start_seconds, seg_duration = segment_boundaries[seg_idx]
            segment_path = os.path.join(tmp_dir, f"segment-{seg_idx:04d}.wav")

            # Cut the segment (whole file is one segment if not chunked).
            if len(segment_boundaries) > 1:
                try:
                    _cut_segment(wav_path, start_seconds, seg_duration, segment_path)
                except RuntimeError:
                    result.timed_out_segments.append(seg_idx)
                    result.warnings.append(
                        f"segment {seg_idx}: failed to cut audio"
                    )
                    continue
            else:
                segment_path = wav_path

            text, success, detected_lang = await _transcribe_segment(
                segment_path, model_path, language, seg_idx
            )

            # Auto-detect feedback: capture detected language on first success.
            if success and detected_lang and result.detected_language is None:
                result.detected_language = detected_lang
                result.warnings.append(
                    f"Whisper detected {detected_lang}. If incorrect, set "
                    f"language explicitly and re-process."
                )

            if success:
                text_accumulator.append(text)
                result.segments_processed += 1
            else:
                result.timed_out_segments.append(seg_idx)

            # Write intra-stage checkpoint after each segment.
            result.text = "\n".join(text_accumulator)
            result.checkpoint_position = seg_idx + 1
            await _write_checkpoint(
                command_id=command_id or "",
                position=seg_idx + 1,
                segments_processed=result.segments_processed,
                timed_out_segments=result.timed_out_segments,
                text_accumulator=text_accumulator,
                detected_language=result.detected_language,
                warnings=result.warnings,
            )

        result.text = "\n".join(text_accumulator)

    # ── Step 5: partial-timeout / extracting_partial handling ─────────
    if result.timed_out_segments:
        result.warnings.append("stt_partial_timeout: some audio segments timed out")
        result.source_status = "extracting_partial"

    if command_id:
        from open_notebook.utils.job_control import report_progress_record

        await report_progress_record(command_id, progress=90, stage="extract")

    logger.info(
        f"STT transcription complete for {command_id or 'unknown'}: "
        f"{result.segments_processed}/{result.segments_total} segments, "
        f"{len(result.timed_out_segments)} timed out, "
        f"detected_language={result.detected_language}"
    )

    return result.to_dict()
