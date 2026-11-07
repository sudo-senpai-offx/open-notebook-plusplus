"""STT abstraction service (D14/D15).

Unified speech-to-text with provider fallback:

1. **whisper.cpp** — local, already in the termux stack, transcribes audio
   offline (port :8090, OpenAI-compatible `/v1/audio/transcriptions`)
2. **browser Web Speech API** — client-side via `webkitSpeechRecognition`;
   reported as available, not executed server-side

On-demand activation: the STT service asks the supervisor to load the
whisper model only when `transcribe()` is called. When local processing is
off or the server is unavailable, reports browser Web Speech as the
client-side option.

Language-aware model selection: Mandarin uses the large tier (small scores
CER 0.222 there), everything else uses the small tier. Malay is explicitly
refused (CER 0.455 on both tiers — mixed-script noise).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.speech import (
    available as speech_available,
)
from open_notebook.speech import (
    transcribe as speech_transcribe,
)

#: Languages that require the large multilingual tier (small is wrong there).
_LARGE_TIER_LANGUAGES = {"zh", "tw"}

#: Languages the models do not speak — refused, not mistranscribed.
_UNSUPPORTED_LANGUAGES = {"my", "ms", "malay"}


@dataclass
class STTResult:
    """Outcome of a speech-to-text transcription request."""

    ok: bool
    text: str = ""
    seconds: float = 0.0
    provider: Optional[str] = None  # whisper.cpp | browser-web-speech
    language: Optional[str] = None
    error: Optional[str] = None
    #: True when the caller should offer the client-side browser option.
    browser_fallback: bool = False


class STTService:
    """Singleton managing STT with provider fallback and on-demand activation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _language_for_model(self, language: Optional[str]) -> str:
        """Map a language code to the model-selection variant.

        Mandarin → large tier; everything else → small tier (default).
        Unsupported → empty string so the caller can refuse explicitly.
        """
        if not language:
            return ""
        lang = language.strip().lower()
        if lang in _UNSUPPORTED_LANGUAGES:
            return ""
        if lang in _LARGE_TIER_LANGUAGES:
            return lang
        return ""

    async def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: Optional[str] = None,
        ensure_resident: bool = True,
    ) -> STTResult:
        """Transcribe an audio or video file to plain text.

        Provider order: whisper.cpp (local) → browser-web-speech (client-side,
        reported but not executed server-side).

        If `language` is declared, the model tier follows from it
        (Mandarin → large, Malay → refused).
        """
        path = Path(audio_path)
        if not path.is_file():
            return STTResult(
                ok=False,
                error=f"no such file: {path}",
                language=language,
            )

        # Refuse unsupported languages explicitly.
        model_lang = self._language_for_model(language)
        if language and language.strip().lower() in _UNSUPPORTED_LANGUAGES:
            return STTResult(
                ok=False,
                error=(
                    f"language '{language}' is not supported by the installed "
                    "models (multilingual tier scores CER 0.455 on it — "
                    "mixed-script noise, not a transcript)"
                ),
                language=language,
            )

        # whisper.cpp (local, via the existing speech client with residency).
        if ensure_resident:
            flags = await ProcessingFlags.get_instance()
            if flags.allow_local_processing:
                ok, detail = await speech_available("stt")
                if ok:
                    result = await speech_transcribe(
                        path,
                        language=model_lang or None,
                        ensure_resident=True,
                    )
                    if result.ok:
                        return STTResult(
                            ok=True,
                            text=result.text,
                            seconds=result.seconds,
                            provider="whisper.cpp",
                            language=language,
                        )
                    # Whisper failed hard; log and fall through to browser.
                    logger.warning(f"whisper.cpp STT failed for {path}: {result.error}")
                else:
                    logger.info(f"whisper.cpp STT not reachable: {detail}")
            else:
                logger.info("allow_local_processing off; skipping whisper.cpp STT")

        # browser-web-speech — client-side only; report availability.
        ok, detail = await speech_available("stt")
        return STTResult(
            ok=False,
            error=(
                "No STT provider available. "
                "Enable 'Allow local processing' for local whisper.cpp, "
                "or use the browser's built-in speech recognition."
            ),
            language=language,
            browser_fallback=True,
        )

    async def supported_languages(self) -> dict[str, list[str]]:
        """Return supported and unsupported language codes."""
        return {
            "supported_large_tier": sorted(_LARGE_TIER_LANGUAGES),
            "supported_small_tier": ["en", "es", "fr", "pt", "de", "it", "ja", "ko"],
            "unsupported": sorted(_UNSUPPORTED_LANGUAGES),
        }


# Module-level singleton.
stt_service = STTService()
