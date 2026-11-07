"""TTS/STT abstraction API (D14/D15).

Endpoints:
  POST   /api/tts/synthesize     — text → audio (piper → edge-tts → browser)
  GET    /api/tts/voices         — list available voices per provider
  POST   /api/stt/transcribe     — audio → text (whisper.cpp → browser)
  GET    /api/stt/languages      — supported/unsupported language codes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.ai.models import ProcessingFlags
from open_notebook.exceptions import OpenNotebookError
from open_notebook.services.stt_service import stt_service
from open_notebook.services.tts_service import tts_service
from open_notebook.speech.client import available as speech_available

router = APIRouter()


class TTSRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize")
    voice: Optional[str] = Field(
        None, description="Voice name (piper stem or edge-tts voice id)"
    )
    language: Optional[str] = Field(
        None, description="BCP-47 language tag (auto-detected if omitted)"
    )
    out_path: Optional[str] = Field(
        None, description="Destination path; generated under podcasts/speech if absent"
    )


class TTSResponse(BaseModel):
    ok: bool
    audio_path: Optional[str] = None
    bytes_written: int = 0
    provider: Optional[str] = None
    language: Optional[str] = None
    voice: Optional[str] = None
    error: Optional[str] = None
    browser_fallback: bool = False


class VoicesResponse(BaseModel):
    piper: List[str] = Field(default_factory=list)
    edge_tts: List[str] = Field(default_factory=list)
    browser_web_speech: List[str] = Field(default_factory=list)


class STTRequest(BaseModel):
    language: Optional[str] = Field(
        None, description="BCP-47 language tag (affects model tier selection)"
    )


class STTResponse(BaseModel):
    ok: bool
    text: Optional[str] = None
    seconds: float = 0.0
    provider: Optional[str] = None
    language: Optional[str] = None
    error: Optional[str] = None
    browser_fallback: bool = False


class LanguagesResponse(BaseModel):
    supported_large_tier: List[str] = Field(default_factory=list)
    supported_small_tier: List[str] = Field(default_factory=list)
    unsupported: List[str] = Field(default_factory=list)


@router.post("/tts/synthesize", response_model=TTSResponse)
async def tts_synthesize(request: TTSRequest):
    """Synthesize text to audio via piper (local) → edge-tts (cloud).

    On-demand: starts the local piper server only when called, and leaves
    residency to the supervisor. When `allow_local_processing` is off,
    falls back to edge-tts cloud. When both fail, reports
    `browser_fallback=True` so the UI can offer client-side Web Speech.
    """
    try:
        out_path = (
            request.out_path or f"/tmp/onb-tts-{abs(hash(request.text)) % 1000000}.wav"
        )
        result = await tts_service.synthesize(
            request.text,
            out_path,
            voice=request.voice,
            language=request.language,
            ensure_resident=True,
        )
        return TTSResponse(
            ok=result.ok,
            audio_path=str(result.audio_path) if result.audio_path else None,
            bytes_written=result.bytes_written,
            provider=result.provider,
            language=result.language,
            voice=result.voice,
            error=result.error,
            browser_fallback=result.browser_fallback,
        )
    except OpenNotebookError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"TTS synthesize error: {e}")
        raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {e}")


@router.get("/tts/voices", response_model=VoicesResponse)
async def tts_voices():
    """List available TTS voices per provider."""
    try:
        voices = await tts_service.list_voices()
        return VoicesResponse(
            piper=voices.get("piper", []),
            edge_tts=voices.get("edge-tts", []),
            browser_web_speech=voices.get("browser-web-speech", []),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"TTS voices error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list voices: {e}")


@router.post("/stt/transcribe", response_model=STTResponse)
async def stt_transcribe(
    file: UploadFile = File(..., description="Audio file to transcribe"),
    language: Optional[str] = Query(
        None, description="BCP-47 language tag (affects model tier)"
    ),
    ensure_resident: bool = Query(
        True, description="Ask the supervisor to load the whisper model"
    ),
):
    """Transcribe an audio file via whisper.cpp (local) → browser (client)."""
    try:
        # Persist the upload to a temp file (whisper-server needs a path).
        import tempfile

        suffix = Path(file.filename or "audio").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            result = await stt_service.transcribe(
                tmp_path,
                language=language,
                ensure_resident=ensure_resident,
            )
            return STTResponse(
                ok=result.ok,
                text=result.text,
                seconds=result.seconds,
                provider=result.provider,
                language=result.language,
                error=result.error,
                browser_fallback=result.browser_fallback,
            )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError:
                pass
    except OpenNotebookError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"STT transcribe error: {e}")
        raise HTTPException(status_code=500, detail=f"STT transcription failed: {e}")


@router.get("/stt/languages", response_model=LanguagesResponse)
async def stt_languages():
    """List supported and unsupported STT language codes."""
    try:
        langs = await stt_service.supported_languages()
        return LanguagesResponse(
            supported_large_tier=langs.get("supported_large_tier", []),
            supported_small_tier=langs.get("supported_small_tier", []),
            unsupported=langs.get("unsupported", []),
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"STT languages error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list languages: {e}")


@router.get("/tts/status", response_model=Dict[str, Any])
async def tts_status():
    """Return TTS server status and local processing flag."""
    flags = await ProcessingFlags.get_instance()
    ok, detail = await speech_available("tts")
    return {
        "allow_local_processing": flags.allow_local_processing,
        "piper_reachable": ok,
        "piper_detail": detail,
    }


@router.get("/stt/status", response_model=Dict[str, Any])
async def stt_status():
    """Return STT server status and local processing flag."""
    flags = await ProcessingFlags.get_instance()
    ok, detail = await speech_available("stt")
    return {
        "allow_local_processing": flags.allow_local_processing,
        "whisper_reachable": ok,
        "whisper_detail": detail,
    }
