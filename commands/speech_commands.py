"""Local text-to-speech as a queueable command.

Why a command and not a request handler
---------------------------------------
The TTS server lives outside the guest and is residency-managed, which means the
call has to be *held* when the device is committed rather than refused. A
synthesis run over a long transcript takes minutes on a phone - minutes during
which the chat server may need the CPU. Only the scheduler can hold and resume
work; an HTTP request cannot be parked for half an hour and still answer the
client that asked.

So the shape mirrors every other slow job here: submit, get held or admitted,
and have the residency client return `held=True` rather than pretending the
device could not do the work.

Why synthesis is its own job and not part of `generate_podcast`
---------------------------------------------------------------
The podcast path is entangled with `podcast_creator`, which brings its own text
to speech and expects to own the whole render. Swapping that out is a separate
change with its own failure modes; this command makes the local voice usable
now - for reading a summary aloud, for an audio track beside a transcript -
without touching a pipeline that currently works.
"""

import time
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.config import PODCASTS_FOLDER
from open_notebook.utils.job_control import (
    get_current_command_id,
    raise_if_cancelled,
    report_progress_record,
)


class SpeechSynthesisInput(CommandInput):
    #: Either literal text or the id of an insight to read. An insight is the
    #: shape that actually gets used - "read this back to me" - and requiring
    #: the caller to fetch and concatenate it first would put a chat-shaped
    #: round trip in front of a five-second job.
    text: Optional[str] = None
    insight_id: Optional[str] = None
    #: Piper voice name, e.g. `en_US-amy-medium`. None uses the server default,
    #: which is the one the phone ships.
    voice: Optional[str] = None
    #: Filename stem under PODCASTS_FOLDER/speech/. Generated when absent, so
    #: two identical requests do not overwrite each other's audio.
    name: Optional[str] = None


class SpeechSynthesisOutput(CommandOutput):
    success: bool
    audio_path: Optional[str] = None
    bytes_written: int = 0
    #: Characters actually rendered. Differs from the request when a model
    #: truncated, which the caller needs to see rather than infer from a file
    #: size.
    characters: int = 0
    processing_time: float = 0.0
    error_message: Optional[str] = None


def _sanitize_stem(raw: str) -> str:
    """A filename stem from caller input, safe on every filesystem we touch.

    The value reaches a path under PODCASTS_FOLDER, so it is reduced to
    alphanumerics, dash, underscore and dot, and the result is capped. A
    caller-supplied `../../etc/passwd` becomes `etcpasswd` - which then fails
    to resolve as a directory and is reported as a bad name, rather than
    reaching outside the podcasts root at all.
    """
    cleaned = "".join(
        char if (char.isalnum() or char in "-_.") else "" for char in raw
    ).strip(".")
    return (cleaned or "speech")[:64]


@command("synthesize_speech", app="open_notebook", retry={"max_attempts": 3})
async def synthesize_speech_command(
    input_data: SpeechSynthesisInput,
) -> SpeechSynthesisOutput:
    """Render text to a WAV file using the local piper server."""
    from open_notebook.speech import synthesize

    start_time = time.time()
    cmd_id = get_current_command_id()

    async def _attempt() -> SpeechSynthesisOutput:
        await report_progress_record(cmd_id, progress=10, stage="collecting_text")
        await raise_if_cancelled(cmd_id)

        body = (input_data.text or "").strip()
        if input_data.insight_id:
            from open_notebook.domain.notebook import SourceInsight
            from open_notebook.exceptions import NotFoundError

            try:
                insight = await SourceInsight.get(input_data.insight_id)
            except NotFoundError as e:
                # Permanent. The record is gone; retrying cannot bring it back,
                # and 3 attempts with backoff would delay every job behind it.
                raise ValueError(
                    f"Insight '{input_data.insight_id}' no longer exists"
                ) from e
            if not insight:
                raise ValueError(f"Insight '{input_data.insight_id}' not found")
            # insight_type first: hearing what kind of thing this is, before the
            # content, is what makes the audio navigable rather than an
            # unattributed wall of words.
            body = "\n\n".join(
                part
                for part in (
                    getattr(insight, "insight_type", "") or "",
                    getattr(insight, "content", "") or "",
                )
                if part
            ).strip()

        if not body:
            # Empty text is the caller's mistake, not a transient condition.
            raise ValueError(
                "Nothing to synthesize: pass `text`, or an `insight_id` whose "
                "content is not empty."
            )

        await report_progress_record(cmd_id, progress=30, stage="synthesizing")
        await raise_if_cancelled(cmd_id)

        stem = _sanitize_stem(input_data.name or f"speech-{uuid.uuid4().hex[:12]}")
        destination = Path(PODCASTS_FOLDER) / "speech" / f"{stem}.wav"

        result = await synthesize(body, destination, voice=input_data.voice)
        if not result.ok:
            if result.held:
                # The device is committed elsewhere. This is the retryable case:
                # a RuntimeError keeps the job in the queue for another attempt
                # rather than burning one of three on a decision that will
                # change. A ValueError here would mark it permanently failed for
                # a reason that is about the phone's moment, not the job.
                raise RuntimeError(f"speech-to-text held: {result.error}")
            raise ValueError(f"Local speech synthesis failed: {result.error}")

        await report_progress_record(cmd_id, progress=100, stage="completed")
        logger.info(
            f"Synthesized {len(body)} chars into {result.path} "
            f"({result.bytes_written} bytes) in {time.time() - start_time:.1f}s"
        )
        return SpeechSynthesisOutput(
            success=True,
            audio_path=str(result.path) if result.path else None,
            bytes_written=result.bytes_written,
            characters=len(body),
            processing_time=time.time() - start_time,
        )

    try:
        return await _attempt()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.opt(exception=True).error(f"Speech synthesis failed: {exc}")
        return SpeechSynthesisOutput(
            success=False,
            processing_time=time.time() - start_time,
            error_message=str(exc),
        )
