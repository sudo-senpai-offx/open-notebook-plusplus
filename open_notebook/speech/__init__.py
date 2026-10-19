"""Local speech: transcription in, synthesis out.

Both halves talk to servers that live outside the proot guest and are
residency-managed there, which is why every entry point asks the supervisor
first and treats a refusal as a hold rather than a fallback. See
`client` for the transport and the reasoning.
"""

from open_notebook.speech.client import (
    DEFAULT_STT_TIMEOUT,
    DEFAULT_STT_URL,
    DEFAULT_TTS_TIMEOUT,
    DEFAULT_TTS_URL,
    SynthesisResult,
    TranscriptionResult,
    available,
    stt_url,
    synthesize,
    transcribe,
    tts_url,
)

__all__ = [
    "DEFAULT_STT_TIMEOUT",
    "DEFAULT_STT_URL",
    "DEFAULT_TTS_TIMEOUT",
    "DEFAULT_TTS_URL",
    "SynthesisResult",
    "TranscriptionResult",
    "available",
    "stt_url",
    "synthesize",
    "transcribe",
    "tts_url",
]
