"""Podcast script generation service (C11).

DeepSeek-web writes a conversational podcast script from source text;
the script can then be rendered to audio via the TTS abstraction.
Variables: source_text, tone, duration, speakers.
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
class PodcastScriptResult:
    """Outcome of podcast script generation."""

    ok: bool
    script: Optional[str] = None
    error: Optional[str] = None


class PodcastScriptGenerator:
    """Generate a multi-speaker podcast script via DeepSeek-web."""

    _PROMPT_TEMPLATE = """You are a podcast scriptwriter. Convert the source text into a natural,
conversational podcast script.

Source text:
{source_text}

Tone: {tone}
Target duration: {duration} minutes
Speakers: {speakers}

Write a script with speaker labels (e.g., "HOST:", "GUEST:") marking each
turn. Include stage directions in brackets where helpful (e.g., [intro music],
[laughter], [pause]). Make it sound spoken, not written: contractions,
fragments, natural transitions.

Output ONLY the script, nothing else. No markdown fences, no introduction,
no conclusion.

Script:"""

    async def generate(
        self,
        source_text: str,
        *,
        notebook_id: str,
        tone: str = "conversational",
        duration: int = 10,
        speakers: str = "Host, Guest",
        classification: str = "podcast",
    ) -> PodcastScriptResult:
        """Generate a podcast script from source text via DeepSeek-web."""
        if not source_text or not source_text.strip():
            return PodcastScriptResult(ok=False, error="no source text")

        prompt = self._PROMPT_TEMPLATE.format(
            source_text=source_text[:12000],
            tone=tone,
            duration=duration,
            speakers=speakers,
        )

        try:
            result = await deepseek_session_service.send_message(
                notebook_id=notebook_id,
                classification=classification,
                message=prompt,
            )
        except OpenNotebookError as exc:
            logger.warning(f"podcast script DeepSeek error: {exc}")
            return PodcastScriptResult(ok=False, error=str(exc))

        script = result.response
        if not script:
            return PodcastScriptResult(
                ok=False, error="empty response from DeepSeek-web"
            )

        # Strip any surrounding markdown fences.
        script = script.strip()
        if script.startswith("```"):
            lines = script.split("\n")
            # Find start after opening fence + optional language hint.
            start = 1
            while start < len(lines) and not lines[start].strip():
                start += 1
            end = len(lines) - 1
            while end > start and lines[end].strip() == "```":
                end -= 1
            script = "\n".join(lines[start : end + 1]).strip()

        return PodcastScriptResult(ok=True, script=script)


# Module-level singleton.
podcast_script_generator = PodcastScriptGenerator()
