"""Transcript post-processing service (C13).

Cleans up STT transcripts via DeepSeek-web (session-aware).
Variables: transcript, language.

Processing steps (research-validated):
- Punctuation restoration
- Capitalization / truecasing
- Filler word removal (um, uh, like, you know)
- False start and self-correction removal
- Number/date formatting
- Paragraph segmentation
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from open_notebook.exceptions import OpenNotebookError
from open_notebook.services.deepseek_session_service import (
    deepseek_session_service,
)


@dataclass
class TranscriptPostProcessResult:
    """Outcome of transcript post-processing."""

    ok: bool
    cleaned_transcript: Optional[str] = None
    original_length: int = 0
    cleaned_length: int = 0
    error: Optional[str] = None


class TranscriptPostProcessor:
    """Post-process STT transcripts via DeepSeek-web."""

    _PROMPT_TEMPLATE = """You are a transcript editor. Clean up the following speech-to-text transcript.

Transcript:
{transcript}

Language: {language}

Tasks:
- Restore punctuation and capitalization
- Remove filler words (um, uh, like, you know)
- Fix false starts and self-corrections
- Format numbers and dates
- Split into paragraphs

Output ONLY the cleaned transcript, nothing else. No preamble, no explanation.

Cleaned transcript:"""

    async def postprocess(
        self,
        transcript: str,
        *,
        notebook_id: str,
        language: str = "en",
        classification: str = "stt",
    ) -> TranscriptPostProcessResult:
        """Post-process an STT transcript via DeepSeek-web.

        Session-aware: uses the notebook's DeepSeek session series so the
        model remembers context across multiple transcript cleanup calls.
        """
        if not transcript or not transcript.strip():
            return TranscriptPostProcessResult(
                ok=False, error="no transcript to process"
            )

        prompt = self._PROMPT_TEMPLATE.format(
            transcript=transcript[:12000],
            language=language,
        )

        try:
            result = await deepseek_session_service.send_message(
                notebook_id=notebook_id,
                classification=classification,
                message=prompt,
            )
        except OpenNotebookError as exc:
            logger.warning(f"transcript post-process DeepSeek error: {exc}")
            return TranscriptPostProcessResult(
                ok=False, error=str(exc), original_length=len(transcript)
            )

        cleaned = result.response
        if not cleaned:
            return TranscriptPostProcessResult(
                ok=False,
                error="empty response from DeepSeek-web",
                original_length=len(transcript),
            )

        cleaned = cleaned.strip()

        return TranscriptPostProcessResult(
            ok=True,
            cleaned_transcript=cleaned,
            original_length=len(transcript),
            cleaned_length=len(cleaned),
        )


# Module-level singleton.
transcript_post_processor = TranscriptPostProcessor()
