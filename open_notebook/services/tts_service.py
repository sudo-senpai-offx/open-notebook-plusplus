"""TTS abstraction service (D14/D15).

Unified text-to-speech with provider fallback:

1. **piper** — local, already in the termux stack, renders audio offline
   (has Japanese voices via the tsukuyomi guest on :8092)
2. **edge-tts** — cloud fallback via Microsoft Edge online TTS, no API key
   (researched: `rany2/edge-tts`, WebSocket/aiohttp, cross-platform incl. Termux)
3. **browser-web-speech** — client-side via `window.speechSynthesis`;
   reported as available, not executed server-side

tts-ja resolution: on-demand only, never a persistent server. The TTS
service starts the local piper server only when `synthesize()` is called,
synthesizes, and leaves residency to the supervisor. When
`allow_local_processing` is off, falls back to edge-tts cloud. When both
are unavailable, reports browser Web Speech as the client-side option.

Multi-phoneme: supports multiple languages/voices — language is detected
from script (CJK → Japanese/Chinese, Latin → English) or passed explicitly.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from open_notebook.ai.models import ProcessingFlags
from open_notebook.speech import (
    DEFAULT_TTS_TIMEOUT,
)
from open_notebook.speech import (
    available as speech_available,
)
from open_notebook.speech import (
    synthesize as speech_synthesize,
)
from open_notebook.speech.client import tts_url as _speech_tts_url

# Edge-TTS voices per language (researched from edge-tts voice list).
_EDGE_TTS_VOICES = {
    "ja": "ja-JP-NanamiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "pt": "pt-BR-FranciscaNeural",
}

#: Default edge-tts voice when language is unknown.
_DEFAULT_EDGE_VOICE = "en-US-AriaNeural"

#: edge-tts is invoked as a subprocess (no persistent server, no API key).
_EDGE_TTS_BIN = os.getenv("EDGE_TTS_BIN", "edge-tts")


def _detect_language(text: str) -> str:
    """Detect the dominant script of `text` and return a BCP-47 language tag.

    Kana → Japanese, CJK ideographs → Chinese, Latin → English, anything
    else → Japanese (the tsukuyomi model's own language).
    """
    import re

    if re.search(r"[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF]", text):
        return "ja"
    if re.search(r"[\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF]", text):
        return "zh"
    if re.search(r"[A-Za-z\xC0-\xFF]", text):
        return "en"
    return "ja"


@dataclass
class TTSResult:
    """Outcome of a TTS synthesis request."""

    ok: bool
    audio_path: Optional[Path] = None
    bytes_written: int = 0
    provider: Optional[str] = None  # piper | edge-tts | browser-web-speech
    language: Optional[str] = None
    voice: Optional[str] = None
    error: Optional[str] = None
    #: True when the caller should offer the client-side browser option.
    browser_fallback: bool = False


class TTSService:
    """Singleton managing TTS with provider fallback and on-demand activation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def synthesize(
        self,
        text: str,
        out_path: str | Path,
        *,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        ensure_resident: bool = True,
    ) -> TTSResult:
        """Render `text` to an audio file at `out_path`.

        Provider order: piper (local) → edge-tts (cloud).
        browser-web-speech is never executed server-side; it is reported
        via `browser_fallback=True` when both server providers fail.

        If `language` is omitted it is detected from the script.
        """
        body = (text or "").strip()
        if not body:
            return TTSResult(ok=False, error="nothing to synthesize")

        if language is None:
            language = _detect_language(body)

        destination = Path(out_path)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return TTSResult(
                ok=False,
                error=f"cannot write to {destination}: {exc}",
                language=language,
            )

        # 1. piper (local, via the existing speech client with residency).
        if ensure_resident:
            flags = await ProcessingFlags.get_instance()
            if flags.allow_local_processing:
                ok, detail = await speech_available("tts")
                if ok:
                    result = await speech_synthesize(
                        body,
                        destination,
                        voice=voice,
                        ensure_resident=True,
                    )
                    if result.ok:
                        return TTSResult(
                            ok=True,
                            audio_path=result.path,
                            bytes_written=result.bytes_written,
                            provider="piper",
                            language=language,
                            voice=voice or "default",
                        )
                    # Piper failed hard; log and fall through to edge-tts.
                    logger.warning(
                        f"piper TTS failed for {destination}: {result.error}"
                    )
                else:
                    logger.info(f"piper TTS not reachable: {detail}")
            else:
                logger.info("allow_local_processing off; skipping piper TTS")

        # 2. edge-tts (cloud, no API key, subprocess, no persistent server).
        try:
            edge_result = await self._synthesize_edge_tts(
                body, destination, voice=voice, language=language
            )
            if edge_result.ok:
                return edge_result
        except Exception as exc:  # noqa: BLE001 - edge-tts is a fallback
            logger.warning(f"edge-tts failed: {exc}")

        # 3. browser-web-speech — client-side only; report availability.
        ok, detail = await speech_available("tts")
        return TTSResult(
            ok=False,
            error=(
                "No TTS provider available. "
                "Enable 'Allow local processing' for local piper, "
                "or ensure internet for edge-tts cloud."
            ),
            language=language,
            voice=voice,
            browser_fallback=True,
        )

    async def _synthesize_edge_tts(
        self,
        text: str,
        out_path: Path,
        *,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> TTSResult:
        """Synthesize via edge-tts subprocess (cloud, no API key)."""
        edge_voice = voice or _EDGE_TTS_VOICES.get(
            language or "en", _DEFAULT_EDGE_VOICE
        )

        # edge-tts CLI: --text, --voice, --write-media
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", prefix="edge-tts-", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                _EDGE_TTS_BIN,
                "--text",
                text,
                "--voice",
                edge_voice,
                "--write-media",
                str(tmp_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=DEFAULT_TTS_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return TTSResult(
                    ok=False,
                    error="edge-tts timed out",
                    language=language,
                    voice=edge_voice,
                )

            if proc.returncode != 0:
                stderr = (
                    (await proc.stderr.read()).decode("utf-8", "replace")
                    if proc.stderr
                    else ""
                )
                return TTSResult(
                    ok=False,
                    error=f"edge-tts exited {proc.returncode}: {stderr[:200]}",
                    language=language,
                    voice=edge_voice,
                )

            if not tmp_path.is_file() or tmp_path.stat().st_size == 0:
                return TTSResult(
                    ok=False,
                    error="edge-tts produced no audio",
                    language=language,
                    voice=edge_voice,
                )

            data = tmp_path.read_bytes()
            out_path.write_bytes(data)
            return TTSResult(
                ok=True,
                audio_path=out_path,
                bytes_written=len(data),
                provider="edge-tts",
                language=language,
                voice=edge_voice,
            )
        except FileNotFoundError:
            return TTSResult(
                ok=False,
                error="edge-tts not installed (pip install edge-tts)",
                language=language,
                voice=edge_voice,
            )
        except Exception as exc:  # noqa: BLE001
            return TTSResult(
                ok=False,
                error=f"edge-tts error: {exc}",
                language=language,
                voice=edge_voice,
            )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except OSError:
                pass

    async def list_voices(self) -> dict[str, list[str]]:
        """Return available voices per provider.

        piper voices come from the local server; edge-tts voices are the
        researched defaults above. browser-web-speech voices are client-side
        and not enumerated here.
        """
        voices: dict[str, list[str]] = {
            "piper": [],
            "edge-tts": [],
            "browser-web-speech": [],
        }

        # piper: query the local server.
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{_speech_tts_url()}/v1/models")
            if response.status_code == 200:
                payload = response.json()
                voices["piper"] = [
                    m.get("id", "") for m in payload.get("data", []) if m.get("id")
                ]
        except Exception:  # noqa: BLE001
            pass

        # edge-tts: researched voice list.
        voices["edge-tts"] = sorted(set(_EDGE_TTS_VOICES.values()))

        return voices


# Module-level singleton (like deepseek_direct_service / deepseek_session_service).
tts_service = TTSService()
