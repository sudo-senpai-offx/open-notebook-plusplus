import asyncio
import hashlib
import operator
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlparse

import content_core as cc
from content_core import ContentCoreConfig, extract_content
from content_core.common import ExtractionOutput
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from loguru import logger
from typing_extensions import Annotated, TypedDict

from open_notebook.ai.models import Model, ModelManager
from open_notebook.domain.content_settings import ContentSettings
from open_notebook.domain.notebook import Asset, Source
from open_notebook.domain.transformation import Transformation
from open_notebook.exceptions import NotFoundError
from open_notebook.graphs.transformation import graph as transform_graph
from open_notebook.storage.blobs import unlink_if_unreferenced
from open_notebook.utils.job_control import (
    get_current_command_id,
    raise_if_cancelled,
    report_progress_record,
)
from open_notebook.utils.runtime_capabilities import (
    docling_available,
    engine_runtime_missing,
)
from open_notebook.preprocess.tools.pdf import extract_pdf
from open_notebook.preprocess.tools.pandoc import convert_document
from open_notebook.preprocess.tools.trafilatura import extract_html
from open_notebook.preprocess.tools.ssconvert import (
    convert_spreadsheet,
    is_csv,
    is_spreadsheet,
    read_csv_native,
)
from open_notebook.preprocess.tools.tesseract import extract_ocr

# content-core >= 2.1 disables its own Loguru logging for library consumers.
# Both the API and the worker import this module before extracting anything,
# so re-enable it here to keep extraction logs in their output.
logger.enable("content_core")

# Default preferred languages for YouTube transcript selection, used when
# ContentSettings.youtube_preferred_languages is unset. content-core's own
# default is only ["en", "es", "pt"]; we keep the broader list Open Notebook has
# always intended so non-English videos still resolve a transcript.
YOUTUBE_PREFERRED_LANGUAGES = [
    "en",
    "pt",
    "es",
    "de",
    "nl",
    "en-GB",
    "fr",
    "hi",
    "ja",
    "zh-CN",
    "zh-TW",
]

# Checkpoint intervals (Week 2 poly-pipeline requirement)
# Extraction: checkpoint every 5 pages; chunking: every 250 chunks; embedding: every 50 chunks.
CHECKPOINT_EXTRACT_PAGES = 5
CHECKPOINT_CHUNK_EVERY = 250
CHECKPOINT_EMBED_EVERY = 50

# Per-stage timeouts (Week 3: Failure #3 fix — large PDF 60s silent timeout).
# Replaces the single 60s execute_command_sync timeout with per-stage limits.
# Gated by env so deployments can tune per device.
STAGE_TIMEOUT_EXTRACT = float(os.getenv("OPEN_NOTEBOOK_STAGE_TIMEOUT_EXTRACT", "300"))
STAGE_TIMEOUT_CHUNK = float(os.getenv("OPEN_NOTEBOOK_STAGE_TIMEOUT_CHUNK", "30"))
STAGE_TIMEOUT_EMBED = float(os.getenv("OPEN_NOTEBOOK_STAGE_TIMEOUT_EMBED", "120"))
STAGE_TIMEOUT_INDEX = float(os.getenv("OPEN_NOTEBOOK_STAGE_TIMEOUT_INDEX", "60"))
# Heavy / deferred lane (OCR, STT): 30 min backstop so 75+ page scanned PDFs
# do not abort mid-OCR. Instant text extraction stays at 300s.
STAGE_TIMEOUT_HEAVY = float(os.getenv("OPEN_NOTEBOOK_STAGE_TIMEOUT_HEAVY", "1800"))


class _ExtractionOutput:
    """Simple extraction output wrapper for native binary tool results.

    normalize_stage expects `extraction.content` and `extraction.title`.
    Native binary wrappers return dicts; this adapts them to the expected
    interface without importing content-core ExtractionOutput.
    """

    def __init__(self, content: str, title: str = "") -> None:
        self.content = content
        self.title = title


class SourceState(TypedDict):
    # Input describing what to extract: url / file_path / content / delete_source.
    content_state: Dict[str, Any]
    # Result of content-core extraction (does NOT echo url/file_path back).
    extraction: ExtractionOutput
    apply_transformations: List[Transformation]
    source_id: str
    notebook_ids: List[str]
    source: Source
    transformation: Annotated[list, operator.add]
    embed: bool


class TransformationState(TypedDict):
    source: Source
    transformation: Transformation


# =====================================================================
# 7-stage source pipeline state (Week 2 poly-pipeline)
#
# Stages: ingest → route → extract → normalize → chunk → embed → index
#
# Each stage reads and writes fields on this shared state. Stages are
# independent nodes so a failure in one stage can be inspected, retried,
# or skipped without re-running earlier stages. LangGraph checkpoints
# after every stage, so a kill -9 mid-pipeline resumes at the boundary
# rather than restarting from ingest.
# =====================================================================

class Source7StageState(TypedDict, total=False):
    # ── Input (set by the caller) ──────────────────────────────────
    content_state: Dict[str, Any]        # url / file_path / content / delete_source
    source_id: str                        # existing Source record id
    notebook_ids: List[str]               # notebooks to associate
    apply_transformations: List[Transformation]  # transformations to run after indexing
    embed: bool                           # whether to embed after indexing

    # ── Stage outputs (written by each stage, read by later ones) ──
    source: Source                        # loaded / updated Source record
    content_type: str                     # "media" | "text" | "pdf" | "url" | "spreadsheet" | "csv" | "image" | "html"
    lane: str                             # "instant" | "heavy" — determines stage timeout
    route_reason: str                     # why this route was chosen
    extraction: ExtractionOutput          # content-core result (stage 3: extract)
    normalized_text: str                  # cleaned text (stage 4: normalize)
    chunks: List[str]                     # text chunks (stage 5: chunk)
    chunk_infos: List[Dict[str, Any]]     # chunk coordinates/meta (stage 5: chunk)
    embeddings: List[List[float]]         # embedding vectors (stage 6: embed)
    embedded_count: int                   # number of embeddings stored (stage 7: index)
    insights_created: int                 # transformations run (stage 7: index)

    # ── Control / observability ────────────────────────────────────
    stage: str                            # last completed stage name
    errors: List[str]                     # non-fatal errors accumulated across stages
    skipped: List[str]                    # stages skipped (e.g. embed when embed=False)


def _usable_engine(engine: str, kind: str) -> str:
    """Return ``engine``, or "auto" when its opt-in runtime is not installed.

    The engine choice is persisted in the database; runtime availability comes
    from environment flags that are re-evaluated on every boot. A redeploy that
    drops OPEN_NOTEBOOK_ENABLE_CRAWL4AI/_DOCLING (or a failed on-demand install)
    therefore leaves a stored selection pointing at an absent runtime, and
    passing it through fails every extraction with no usable diagnostic. Falling
    back to content-core's "auto" chain keeps ingestion working, loudly.
    """
    missing_env_var = engine_runtime_missing(engine)
    if missing_env_var is None:
        return engine
    logger.warning(
        f"Configured {kind} engine '{engine}' is selected in Content Settings but "
        f"its runtime is not available in this container; falling back to 'auto'. "
        f"Set {missing_env_var}=true to enable it (see ADR-007)."
    )
    return "auto"


def _is_youtube_url(url: str) -> bool:
    """Whether ``url`` points at YouTube (by hostname, not substring)."""
    host = (urlparse(url).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")


_YOUTUBE_NO_TRANSCRIPT_MESSAGE = (
    "Could not extract content from this YouTube video. "
    "No transcript or subtitles are available. "
    "Try configuring a Speech-to-Text model in Settings "
    "to transcribe the audio instead."
)


def _extraction_error(error: "cc.ContentCoreError", url: str) -> ValueError:
    """Turn a content-core extraction error into a user-facing permanent failure.

    content-core >= 2.2 raises typed errors instead of returning empty content.
    They are all treated as permanent (ValueError is in process_source's
    stop_on): content-core already retries transient failures internally,
    including NetworkError, and our worker's 15 attempts exist for SurrealDB
    transaction conflicts, not for re-fetching an unreachable page. A failed
    source can still be retried from the UI.

    The message is fixed per type: content-core's own text can carry proxy
    credentials, local paths or configuration details, so it only goes to the
    worker log (logged by the caller, and kept as the exception's cause).
    """

    if isinstance(error, cc.NoTranscriptFound):
        return ValueError(_YOUTUBE_NO_TRANSCRIPT_MESSAGE)
    # content-core reports a failure of both YouTube transcript paths (e.g.
    # IpBlocked) as "YouTube transcript extraction failed ..."; other
    # ExternalServiceErrors on a YouTube URL (speech-to-text provider, fetch
    # engine) get the generic message below.
    if (
        isinstance(error, cc.ExternalServiceError)
        and url
        and _is_youtube_url(url)
        and "youtube transcript" in str(error).lower()
    ):
        return ValueError(
            "YouTube blocked or failed the transcript request. If this keeps "
            "happening, set CCORE_YOUTUBE_PROXY (a residential proxy) or "
            "CCORE_YOUTUBE_COOKIES_FILE for the worker."
        )
    if isinstance(error, cc.NotFoundError):
        return ValueError(
            "The page was not found (it may have been removed or moved). Check the URL."
        )
    if isinstance(error, cc.NetworkError):
        return ValueError(
            "Could not reach this address (connection, timeout or DNS error). "
            "Check the URL and try again."
        )
    if isinstance(error, cc.InvalidInputError):
        return ValueError("This URL or input is not valid.")
    if isinstance(error, cc.UnsupportedTypeException):
        return ValueError("This file type is not supported.")
    if isinstance(error, cc.FileOperationError):
        return ValueError(
            "The file could not be read. It may be corrupted or in an "
            "unsupported format."
        )
    if isinstance(error, cc.ConfigurationError):
        return ValueError(
            "Content extraction is not configured correctly. Check the content "
            "processing engine and speech-to-text settings; the worker log has "
            "the details."
        )
    if isinstance(error, cc.ExternalServiceError):
        return ValueError("The content extraction service failed.")
    return ValueError("Could not extract content from this source.")


_SCANNED_PDF_MESSAGE = (
    "This PDF has no text layer on the sampled pages, so it looks scanned or "
    "image-only. Turn on OCR to read it: enable the Docling document engine "
    "(set OPEN_NOTEBOOK_ENABLE_DOCLING=true for the worker), then choose "
    "Docling under Settings \u2192 Content Processing and retry this source."
)


def _empty_file_message(file_path: str | None, content_state: dict) -> str:
    """Say what was actually tried, not what a setting could do.

    The previous message told every failure to enable the Docling
    document engine - including on a phone where Docling is deliberately
    absent and the lightweight OCR path exists precisely instead of it.
    A message that names the wrong remedy sends the operator to a
    settings page that cannot help.
    """
    from open_notebook.ocr import available

    # Unavailability outranks attempt: an OCR run that could never start
    # (no binary, flag off) must say so, not report that it "produced
    # nothing" as though the document were at fault.
    ok, detail = available()
    if not ok:
        # Speech is checked first for media: an audio file never reaches OCR,
        # so naming OCR here would name a fallback this file cannot take.
        if _is_media(file_path):
            return _media_failure_message(
                file_path or "", attempted=bool(content_state.get("_stt_attempted"))
            )
        return (
            "Could not extract any text content from this file, and OCR "
            f"is unavailable here: {detail}."
        )
    if _is_media(file_path):
        return _media_failure_message(
            file_path or "", attempted=bool(content_state.get("_stt_attempted"))
        )
    if content_state.get("_ocr_attempted"):
        return (
            "Could not extract any text content from this file. Text "
            "extraction came back empty and OCR produced nothing usable - "
            "the document may be image-only with unreadable scans, or an "
            "encoding neither engine reads."
        )
    return (
        "Could not extract any text content from this file. It may be "
        "empty or in a format the current engine can't read."
    )


async def _try_ocr(file_path: str, cmd_id: str | None = None) -> str:
    """Read a scanned PDF with the lightweight engine.

    Returns empty string rather than raising: OCR being unavailable is a
    degraded outcome, not a failure, and the caller's fallback is the
    existing actionable error.
    """
    from open_notebook.ocr import available, ocr_document

    ok, detail = available()
    if not ok:
        logger.info(f"OCR unavailable, falling back to the scanned-PDF error: {detail}")
        return ""
    try:
        if cmd_id:
            await report_progress_record(cmd_id, progress=20, stage="ocr_started")
        result = await asyncio.to_thread(ocr_document, file_path)
    except Exception as exc:  # noqa: BLE001 - never fail the import over OCR
        logger.warning(f"OCR failed for {file_path}: {type(exc).__name__}: {exc}")
        return ""
    if not result.ok:
        if result.error:
            logger.warning(f"OCR produced nothing for {file_path}: {result.error}")
        return ""
    logger.info(
        f"OCR read {result.pages} page(s) in {result.seconds:.1f}s "
        f"(mean confidence {result.mean_confidence:.0f}, {len(result.text)} chars)"
    )
    return result.text


async def _augment_with_ocr(
    file_path: str, base_text: str, cmd_id: str | None = None
) -> str:
    """Add text that only exists inside the document's figures.

    The selective half. A research paper is mostly typeset text that the
    extractor reads perfectly; OCR over the whole document would be
    wasted CPU and slightly worse output. So only the pages and regions
    the analysis flagged - figure images, scanned inserts - are read, and
    whatever survives the confidence gate is appended.
    """
    from open_notebook.ocr import analyse, available, ocr_document

    ok, detail = available()
    if not ok:
        return base_text
    try:
        analysis = await asyncio.to_thread(analyse, file_path)
        if not analysis.readable or not analysis.needs_ocr:
            return base_text
        result = await asyncio.to_thread(ocr_document, file_path, analysis)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"selective OCR failed for {file_path}: {type(exc).__name__}: {exc}"
        )
        return base_text

    if not result.ok:
        return base_text
    logger.info(
        f"Selective OCR added {len(result.text)} chars from "
        f"{result.pages} figure page(s) of {Path(file_path).name}"
    )
    if not base_text.strip():
        return result.text
    return (
        base_text.rstrip()
        + "\n\n[Text recovered from figures]\n\n"
        + result.text.strip()
    )


#: Magic-byte prefixes for audio and video containers. Checked by content rather
#: than by name for the reason `_is_pdf` documents: the blob store hands files
#: back as `{sha}.bin`, so a lecture recording synced from a phone has no suffix
#: at all and every `endswith(".mp3")` gate would skip it.
_MEDIA_SIGNATURES: tuple[bytes, ...] = (
    b"ID3",  # mp3 with an ID3 tag
    b"RIFF",  # wav / avi - the WAVE/AVI form tag is checked below
    b"OggS",  # ogg / opus / webm audio
    b"fLaC",  # flac
    b"\x1a\x45\xdf\xa3",  # matroska / webm
    b"\x00\x00\x01\xba",  # mpeg program stream
    b"\x00\x00\x01\xb3",  # mpeg video
)


def _is_media(file_path: str | None) -> bool:
    """Whether the file at *file_path* is audio or video, by content.

    The counterpart to `_is_pdf`, and needed for the same reason. A text
    extractor has no answer for a media file except an empty string or an
    "unsupported type" error, and both mean "ask the speech model" rather than
    "this document is unreadable" - so the decision has to be made from the
    bytes before the failure is reported.
    """
    if not file_path:
        return False
    try:
        with open(file_path, "rb") as handle:
            head = handle.read(12)
    except OSError:
        return False
    if not head:
        return False
    if head[:4] == b"RIFF":
        return head[8:12] == b"WAVE"
    if head[4:8] == b"ftyp":
        # mp4 / m4a / mov / 3gp all carry a brand here.
        return True
    if head[0] == 0xFF:
        # MPEG audio/visual frame sync is eleven set bits: 0xFF followed by the
        # top three bits of the next byte. This is also the branch that has to
        # *not* match a JPEG, whose marker byte 0xD8 carries 0b110 in the top
        # three bits - so a photo cannot be handed to whisper, which would
        # answer with an empty transcript and an error naming a file that was
        # never audio.
        return (head[1] & 0xE0) == 0xE0
    return any(head.startswith(sig) for sig in _MEDIA_SIGNATURES)


def _audio_duration_seconds(file_path: str) -> Optional[float]:
    """Probe audio duration via ffprobe. Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


async def _try_stt(
    file_path: str, cmd_id: str | None = None, language: str = ""
) -> str:
    """Transcribe a media file with the local speech server.

    Raises ValueError on failure so the graph's compensation handler can
    clean up partial state and retry. Returning empty string on failure
    (the old behavior) created empty sources with no error - Failure #1
    from the audit. Raising here ensures the failure is visible and
    recoverable via the poly-pipeline's compensation edges.
    """
    from open_notebook.speech import transcribe

    if cmd_id:
        await report_progress_record(cmd_id, progress=20, stage="transcribing")
    result = await transcribe(file_path, language=language or None)
    if not result.ok:
        if result.held:
            logger.info(
                f"speech-to-text held for {Path(file_path).name}: {result.error}"
            )
        else:
            logger.warning(
                f"speech-to-text failed for {Path(file_path).name}: {result.error}"
            )
        raise ValueError(
            f"speech-to-text failed for {Path(file_path).name}: {result.error}"
        )
    logger.info(
        f"Transcribed {len(result.text)} chars from {Path(file_path).name} "
        f"with local speech-to-text"
    )
    return result.text


def _media_failure_message(file_path: str, attempted: bool) -> str:
    """Say what was tried on a media file, not what a setting could do.

    Mirrors `_empty_file_message`'s contract: a remedy that names the wrong
    thing sends the operator to a page that cannot help.
    """
    ok, detail = _speech_reachable()
    if not ok:
        return (
            f"Could not read any text from this audio/video file, and local "
            f"speech-to-text is unavailable here: {detail}."
        )
    if attempted:
        return (
            "Could not read any text from this audio/video file. Text "
            "extraction returned nothing and the speech model produced no "
            "transcript - the file may contain no speech, or the format may "
            "be one the server cannot decode."
        )
    return (
        "Could not read any text from this audio/video file. The speech "
        "server could not be reached, so nothing was transcribed."
    )


def _speech_reachable() -> tuple[bool, str]:
    """Can the speech server be reached from here?

    `speech.available()` is a coroutine because its probe is HTTP. Building an
    error message must not become an await point in the middle of an
    extraction's failure path, so this does the same reachability check with a
    socket - fast, local, and honest about the thing the message claims: that
    the server is not reachable *from here*.

    TCP-connectable, not answering. That is the right strength for a message
    that says "the speech server could not be reached", and a server that is
    listening but not yet loaded is exactly the state the residency reaper can
    leave behind, which is not what this message is about.
    """
    import socket
    from urllib.parse import urlparse as _urlparse

    from open_notebook.speech import stt_url

    url = stt_url()
    parsed = _urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8090
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True, f"speech-to-text at {url}"
    except OSError as exc:
        return False, f"{url} unreachable ({type(exc).__name__})"


async def _extract_with_text_engine(
    cmd_id: str | None,
    content_state: dict,
    config: Optional[ContentCoreConfig],
    config_kwargs: dict,
    url: str,
    file_path: str | None,
    language: str = "",
):
    """Run content-core, then read whatever only the figures contain.

    The selective half of OCR. A research paper is mostly typeset text
    that this already reads perfectly; OCR over the whole document would
    be wasted CPU and slightly worse output, while skipping the figures
    loses real content. So only the pages and regions the analysis
    flagged get read, and the result is appended.

    Media files get a third ending: an audio or video source that the text
    extractor cannot read is handed to the local speech server instead of
    failing, which is what makes a recorded lecture searchable.
    """
    # Set only when content-core refused a media file. Carried out of the
    # `except` block so the speech fallback below can explain a joint failure
    # with the cause attached, rather than reporting the speech outcome alone.
    core_error: Optional[cc.ContentCoreError] = None
    await report_progress_record(cmd_id, progress=25, stage="extracting_content")
    try:
        # Fast profile when Docling is explicitly chosen: the heavy
        # passes are what make it impractical on a phone.
        if config_kwargs.get("document_engine") == "docling" and os.getenv(
            "OPEN_NOTEBOOK_DOCLING_FAST_PROFILE", "true"
        ).lower() in ("1", "true", "yes", "on"):
            config_kwargs.setdefault("docling_formulas", False)
            config_kwargs.setdefault("docling_vision", False)

        processed = await extract_content(
            url=content_state.get("url"),
            file_path=content_state.get("file_path"),
            content=content_state.get("content"),
            config=config,
        )
        await report_progress_record(cmd_id, progress=35, stage="extraction_done")

        if (
            file_path
            and _is_pdf(file_path)
            and processed.content
            and processed.content.strip()
        ):
            augmented = await _augment_with_ocr(file_path, processed.content, cmd_id)
            if augmented != processed.content:
                processed.content = augmented
        elif (
            file_path
            and _is_pdf(file_path)
            and not content_state.get("skip_extraction")
        ):
            # Last resort. The text-layer probe above said readable (or
            # was uncertain, which it reports as readable), the engine
            # found nothing, and failing now would discard a document OCR
            # can read. This is the path that saved a scanned PDF on the
            # phone after the probe and the extractor disagreed about it.
            ocr_text = await _try_ocr(file_path, cmd_id)
            content_state["_ocr_attempted"] = True
            if ocr_text and ocr_text.strip():
                logger.info(
                    f"OCR recovered {len(ocr_text)} chars the text engine "
                    f"missed in {Path(file_path).name}"
                )
                processed.content = ocr_text
    except cc.ContentCoreError as e:
        logger.warning(f"content-core extraction failed ({type(e).__name__}): {e}")
        if not (file_path and _is_media(file_path)):
            raise _extraction_error(e, url) from e
        # A text extractor declining a media file is the expected answer, not a
        # fault: `UnsupportedTypeException` and `FileOperationError` are what a
        # lecture recording produces, and converting them into a permanent
        # "this file type is not supported" would throw away the one thing that
        # can read it. Swallow the error here and let the speech fallback below
        # decide, so an unreachable speech server produces an actionable
        # message instead of this one.
        core_error = e
        processed = ExtractionOutput(content="", title="")

    # Speech fallback. Runs after the try/except so it covers both shapes of the
    # same thing — an extractor that returned nothing and one that refused the
    # file outright — and only when the file is actually media, because for
    # everything else an empty extraction means an empty document.
    if (
        file_path
        and _is_media(file_path)
        and not content_state.get("skip_extraction")
        and not processed.content.strip()
    ):
        content_state["_stt_attempted"] = True
        transcript = await _try_stt(file_path, cmd_id, language)
        if transcript.strip():
            processed.content = transcript
        elif core_error is not None:
            # The extractor refused the file *and* the speech server could not
            # read it either. Say so together: reporting only the first would
            # send the operator to configure a content engine when the missing
            # piece was speech-to-text.
            raise ValueError(
                _media_failure_message(file_path, attempted=True)
            ) from core_error

    return processed


def _is_pdf(file_path: str | None) -> bool:
    """Whether the file at *file_path* is a PDF, by content, not by name.

    The blob store does not guarantee suffixes: files received over the
    channel are stored as `{sha}.bin` (the PUT endpoint is never told the
    original filename), so gating OCR paths on `endswith(".pdf")` silently
    skipped every synced blob. A scanned PDF named `.bin` fell through
    all three OCR gates in two seconds and failed as "unreadable". Magic
    bytes do not depend on what anyone named the file.
    """
    if not file_path:
        return False
    try:
        with open(file_path, "rb") as handle:
            head = handle.read(5)
    except OSError:
        # A path that cannot be opened is not a PDF, whatever its
        # suffix claims. The suffix short-circuit below must not run
        # first, or a missing file named `.pdf` counts as one.
        return False
    return str(file_path).lower().endswith(".pdf") or head == b"%PDF-"


def _is_image(file_path: str | None) -> bool:
    """Whether the file at *file_path* is an image, by content, not by name.

    The blob store stores files as ``{sha}.bin``, so gating OCR on
    ``endswith((".png", ".jpg"))`` silently skips synced blobs. Magic bytes
    detect the format regardless of what anyone named the file.

    Covers JPEG, PNG, GIF, WebP, TIFF, BMP.
    """
    if not file_path:
        return False
    try:
        with open(file_path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if not head:
        return False
    if head[:3] == b"\xff\xd8\xff":
        return True  # JPEG
    if head[:4] == b"\x89PNG":
        return True  # PNG
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True  # GIF
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True  # WebP
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return True  # TIFF (little/big endian)
    if head[:2] == b"BM":
        return True  # BMP
    return False


def _pdf_engine_uses_ocr(document_engine: str) -> bool:
    """Whether PDFs will be routed through Docling (and therefore OCR'd).

    content-core's ``auto`` routing only prefers Docling when its runtime is
    installed, so a missing runtime means a scanned PDF has no OCR fallback and
    would extract to empty content.
    """
    return document_engine == "docling" or (
        document_engine == "auto" and docling_available()
    )


def _pdf_text_layer_present(file_path: str) -> bool:
    """Whether a PDF has extractable text on its first, middle or last page.

    A cheap, read-only probe used to fail fast on scanned/image-only PDFs before
    extracting every page. Sampling spread pages avoids both a full parse and a
    false "scanned" verdict on documents that merely start with a cover image.
    Returns True on any uncertainty (unreadable file, missing pdfplumber) so the
    real extraction — not this heuristic — makes the call.
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:  # pragma: no cover - pdfplumber ships with content-core
        return True
    try:
        with pdfplumber.open(file_path) as pdf:
            pages = pdf.pages
            if not pages:
                return True
            sample = {0, len(pages) // 2, len(pages) - 1}
            return any((pages[i].extract_text() or "").strip() for i in sample)
    except Exception as e:
        logger.debug(f"PDF text-layer probe failed for {file_path}: {e}")
        return True


async def content_process(state: SourceState) -> dict:
    from open_notebook.utils.job_control import (
        get_current_command_id,
        raise_if_cancelled,
        report_progress_record,
    )

    content_state: Dict[str, Any] = state["content_state"]
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=10, stage="extracting")
    await raise_if_cancelled(cmd_id)

    # content-core 2.x takes engine/model overrides via ContentCoreConfig
    # (keyword-only), not inside the input dict.
    config_kwargs: Dict[str, Any] = {
        "youtube_languages": YOUTUBE_PREFERRED_LANGUAGES,
    }

    # Honor the persisted content-processing engine choices. content-core
    # accepts "auto"/"simple"/"firecrawl"/"jina"/"crawl4ai" for URLs and
    # "auto"/"docling"/"simple" for documents; falling back to "auto" keeps the
    # previous behavior when settings are unset.
    try:
        settings: ContentSettings = await ContentSettings.get_instance()  # type: ignore[assignment]
        if settings.youtube_preferred_languages:
            config_kwargs["youtube_languages"] = settings.youtube_preferred_languages
        if settings.default_content_processing_engine_url:
            config_kwargs["url_engine"] = _usable_engine(
                settings.default_content_processing_engine_url, "url"
            )
        if settings.default_content_processing_engine_doc:
            config_kwargs["document_engine"] = _usable_engine(
                settings.default_content_processing_engine_doc, "document"
            )
        if settings.docling_ocr is not None:
            config_kwargs["docling_ocr"] = settings.docling_ocr
        if settings.docling_formulas is not None:
            config_kwargs["docling_formulas"] = settings.docling_formulas
        if settings.docling_vision is not None:
            config_kwargs["docling_vision"] = settings.docling_vision
    except Exception as e:
        # Keep the server-side traceback for diagnosing DB/deserialization
        # failures while still falling back to defaults (non-fatal).
        logger.opt(exception=True).warning(
            f"Failed to load content settings, using defaults: {e}"
        )

    try:
        await report_progress_record(cmd_id, progress=15, stage="loading_models")
        model_manager = ModelManager()
        defaults = await model_manager.get_defaults()
        if defaults.default_speech_to_text_model:
            stt_model = await Model.get(defaults.default_speech_to_text_model)
            if stt_model:
                config_kwargs["audio_provider"] = stt_model.provider
                config_kwargs["audio_model"] = stt_model.name
                logger.debug(
                    f"Using speech-to-text model: {stt_model.provider}/{stt_model.name}"
                )
        await report_progress_record(cmd_id, progress=20, stage="models_ready")
    except Exception as e:
        logger.warning(f"Failed to retrieve speech-to-text model configuration: {e}")
        # Continue without custom audio model (content-core will use its default)

    # Heartbeat early during potentially long config/model init
    await report_progress_record(cmd_id, progress=12, stage="initializing")

    config = ContentCoreConfig(**config_kwargs) if config_kwargs else None

    # Log the effective extraction engines so operators can confirm which engine
    # actually ran (content-core logs its own dispatch only at DEBUG). Absent
    # overrides fall back to content-core's "auto".
    if content_state.get("url"):
        target = "url"
    elif content_state.get("file_path"):
        target = "document"
    else:
        target = "content"
    logger.info(
        f"Extracting {target} via content-core "
        f"(url_engine={config_kwargs.get('url_engine', 'auto')}, "
        f"document_engine={config_kwargs.get('document_engine', 'auto')}, "
        f"docling_ocr={config_kwargs.get('docling_ocr', 'auto')}, "
        f"docling_formulas={config_kwargs.get('docling_formulas', 'auto')}, "
        f"docling_vision={config_kwargs.get('docling_vision', 'auto')})"
    )

    # Fail fast on scanned/image-only PDFs when no OCR engine is in play: the
    # full extraction would read every page only to return empty content, and
    # the generic "could not extract" error hides the real, actionable cause.
    file_path = content_state.get("file_path")
    if (
        file_path
        and _is_pdf(file_path)
        and not _pdf_engine_uses_ocr(config_kwargs.get("document_engine", "auto"))
        and not await asyncio.to_thread(_pdf_text_layer_present, file_path)
    ):
        # Try the lightweight engine before giving up. Docling is the
        # heavyweight answer and the wrong one for a phone: it needs
        # multi-hundred-megabyte models and will be killed by the OOM
        # killer next to two llama-servers. Tesseract is ~30 MB and
        # reads this page in about a second.
        ocr_text = await _try_ocr(file_path, cmd_id)
        content_state["_ocr_attempted"] = True
        if ocr_text:
            content_state["ocr_text"] = ocr_text
            content_state["skip_extraction"] = True
        else:
            logger.warning(
                f"PDF {file_path} has no text layer and no OCR engine produced "
                "text; failing fast with an actionable message."
            )
            raise ValueError(_SCANNED_PDF_MESSAGE)

    # OCR produced the whole document, so the text extractor is skipped:
    # it would read the same pages and return empty content. Validation,
    # cleanup and the return are shared with the normal path, so this
    # falls through rather than returning early.
    url = content_state.get("url") or ""

    if content_state.get("skip_extraction"):
        from content_core import ExtractionOutput

        processed = ExtractionOutput(content=content_state["ocr_text"], title="")
        await report_progress_record(cmd_id, progress=35, stage="extraction_done")
    else:
        # The source's declared language, when it has one. It decides the
        # model tier and stops the model guessing - see _try_stt.
        _lang = getattr(state.get("source"), "language", "") or ""
        processed = await _extract_with_text_engine(
            cmd_id, content_state, config, config_kwargs, url, file_path, _lang
        )

    # Since content-core 2.2, empty content means the source was genuinely
    # empty; extraction failures raise (handled above).
    if not processed.content or not processed.content.strip():
        if url and _is_youtube_url(url):
            raise ValueError(_YOUTUBE_NO_TRANSCRIPT_MESSAGE)
        if content_state.get("file_path"):
            raise ValueError(
                _empty_file_message(content_state.get("file_path"), content_state)
            )
        raise ValueError(
            "Could not extract any text content from this source. "
            "The content may be empty, inaccessible, or in an unsupported format."
        )

    # content-core 2.x no longer deletes the uploaded source file after
    # extraction (the delete_source flag it used to honor is gone). Preserve the
    # previous auto-delete behavior on our side.
    #
    # Reference-aware: content-addressed storage means two sources can
    # share one blob, and a bare unlink here would delete bytes the other
    # source still needs - losing a file *during* extraction, with the
    # source row left pointing at a path that no longer exists.
    if content_state.get("delete_source") and content_state.get("file_path"):
        file_path = content_state["file_path"]
        sha256 = content_state.get("sha256")
        try:
            if sha256:
                removed = await unlink_if_unreferenced(file_path, sha256)
                if not removed and os.path.exists(file_path):
                    logger.info(
                        f"Kept {file_path} after extraction: still referenced "
                        "by another source"
                    )
            else:
                os.unlink(file_path)
        except FileNotFoundError:
            logger.warning(f"File not found while trying to delete: {file_path}")
        except Exception as e:
            logger.warning(f"Failed to delete source file {file_path}: {e}")

    return {"extraction": processed}


async def save_source(state: SourceState) -> dict:
    cmd_id = get_current_command_id()
    await raise_if_cancelled(cmd_id)
    content_state = state["content_state"]
    extraction = state["extraction"]

    # Get existing source using the provided source_id
    source = await Source.get(state["source_id"])
    if not source:
        raise ValueError(f"Source with ID {state['source_id']} not found")

    # Update the source with processed content. content-core's ExtractionOutput
    # does not echo url/file_path back, so carry them from the input state.
    #
    # Preserve the existing asset's content identity (sha256, filename,
    # size). Rebuilding Asset() from scratch would drop it: the blob is
    # named by hash, so losing the hash loses the only record of which
    # bytes this source was built from, and losing the filename means the
    # download is served as "<sha>.pdf".
    previous = source.asset or Asset()
    source.asset = Asset(
        url=content_state.get("url"),
        file_path=content_state.get("file_path"),
        sha256=previous.sha256,
        filename=content_state.get("filename") or previous.filename,
        size=previous.size,
    )
    source.full_text = extraction.content

    # Preserve user-set title; only overwrite placeholder or empty titles
    if extraction.title and (not source.title or source.title == "Processing..."):
        source.title = extraction.title

    await source.save()

    # NOTE: Notebook associations are created by the API immediately for UI responsiveness
    # No need to create them here to avoid duplicate edges

    if state["embed"]:
        if source.full_text and source.full_text.strip():
            logger.debug("Embedding content for vector search")
            await source.vectorize()
        else:
            logger.warning(
                f"Source {source.id} has no text content to embed, skipping vectorization"
            )

    await report_progress_record(cmd_id, progress=40, stage="extracted")
    await report_progress_record(cmd_id, progress=55, stage="saved")
    return {"source": source}


def trigger_transformations(state: SourceState, config: RunnableConfig) -> List[Send]:
    if len(state["apply_transformations"]) == 0:
        return []

    to_apply = state["apply_transformations"]
    logger.debug(f"Applying transformations {to_apply}")

    return [
        Send(
            "transform_content",
            {
                "source": state["source"],
                "transformation": t,
            },
        )
        for t in to_apply
    ]


async def transform_content(state: TransformationState) -> Optional[dict]:
    cmd_id = get_current_command_id()
    await raise_if_cancelled(cmd_id)
    await report_progress_record(cmd_id, progress=70, stage="transforming")
    source = state["source"]
    content = source.full_text
    # Whitespace-only text would hit the transformation graph's empty-content
    # guard; skip it here like empty text.
    if not content or not content.strip():
        return None
    transformation: Transformation = state["transformation"]

    logger.debug(f"Applying transformation {transformation.name}")
    # LangGraph accepts a partial state dict at runtime, but its typed
    # overloads require the full state type (langgraph typing limitation).
    result = await transform_graph.ainvoke(  # type: ignore[call-overload]
        dict(input_text=content, transformation=transformation),
        config=RunnableConfig(configurable={"model_id": transformation.model_id}),
    )
    await source.add_insight(transformation.title, result["output"])
    await report_progress_record(cmd_id, progress=75, stage="transformed")
    return {
        "transformation": [
            {
                "output": result["output"],
                "transformation_name": transformation.name,
            }
        ]
    }


# =====================================================================
# 7-stage pipeline nodes (Week 2 poly-pipeline)
#
# Each node is a self-contained stage that reads from and writes to
# Source7StageState. They can be composed into the 7-stage graph below
# or called individually for testing / partial re-runs.
# =====================================================================

async def ingest_stage(state: Source7StageState) -> dict:
    """Stage 1: Ingest — validate input, load the Source record.

    Reads content_state (url / file_path / content / delete_source)
    and source_id from the input. Loads the existing Source record.
    Does NOT touch the file system or network — pure validation + DB read.
    """
    from open_notebook.database.repository import ensure_record_id

    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=5, stage="ingest")
    await raise_if_cancelled(cmd_id)

    content_state = state.get("content_state") or {}
    source_id = state.get("source_id")
    if not source_id:
        raise ValueError("source_id is required for ingestion")

    try:
        source = await Source.get(source_id)
    except NotFoundError as exc:
        raise ValueError(
            f"Source '{source_id}' no longer exists (deleted before processing?)"
        ) from exc
    if not source:
        raise NotFoundError(f"Source '{source_id}' not found")

    # Update source with command reference for cancellation tracking
    if cmd_id:
        source.command = ensure_record_id(cmd_id)
    await source.save()

    return {
        "source": source,
        "content_state": content_state,
        "stage": "ingest",
    }


async def route_stage(state: Source7StageState) -> dict:
    """Stage 2: Route — classify content and choose the pipeline path.

    Inspects content_state to decide:
      - media (audio/video) → speech-to-text path (heavy lane)
      - pdf → OCR-capable extraction path (heavy lane)
      - url → web extraction path (instant lane)
      - text / other → direct text path (instant lane)
      - spreadsheet (.xlsx/.xls/.ods) → ssconvert path (instant lane)
      - csv → native UTF-8 text read (instant lane)
      - image → tesseract OCR path (heavy lane)

    Sets content_type, lane, and route_reason for downstream stages.
    ``lane`` determines the extract stage timeout: "instant" = 300s,
    "heavy" = 1800s (OCR/STT backstop).
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=15, stage="route")
    await raise_if_cancelled(cmd_id)

    content_state = state.get("content_state") or {}
    file_path = content_state.get("file_path")
    url = content_state.get("url") or ""
    content = content_state.get("content")

    # Media files (by magic bytes, not suffix — blob store uses {sha}.bin)
    if file_path and _is_media(file_path):
        source = state.get("source")
        language = getattr(source, "language", None) if source else None

        # Short audio (<60s) requires an explicit language — whisper's
        # auto-detection is unreliable on short clips and produces
        # mixed-script noise on Tamil/Malay. Long audio may auto-detect.
        if not language:
            duration = _audio_duration_seconds(file_path)
            if duration is not None and duration < 60:
                raise ValueError(
                    "Short audio (<60s) requires an explicit language. "
                    "Set the source's language field (two-letter code, e.g. "
                    "'en', 'zh', 'ja', 'ta') before processing."
                )
            route_reason = (
                f"media file detected by magic bytes"
                + (f" ({duration:.0f}s, language will be auto-detected)" if duration else "")
            )
        else:
            route_reason = f"media file detected by magic bytes (language={language})"

        return {
            "content_type": "media",
            "lane": "heavy",
            "route_reason": route_reason,
            "stage": "route",
        }

    # Image files (by magic bytes) → tesseract OCR path (heavy lane)
    if file_path and _is_image(file_path):
        return {
            "content_type": "image",
            "lane": "heavy",
            "route_reason": "image file detected by magic bytes",
            "stage": "route",
        }

    # Spreadsheet files → ssconvert path (instant lane — fast conversion)
    if file_path and is_spreadsheet(file_path):
        return {
            "content_type": "spreadsheet",
            "lane": "instant",
            "route_reason": "spreadsheet file detected by extension",
            "stage": "route",
        }

    # CSV files → native UTF-8 text read (instant lane — no binary needed)
    if file_path and is_csv(file_path):
        return {
            "content_type": "csv",
            "lane": "instant",
            "route_reason": "CSV file detected by extension",
            "stage": "route",
        }

    # URL input
    if url and not file_path and not content:
        return {
            "content_type": "url",
            "lane": "instant",
            "route_reason": "url input, no local file",
            "stage": "route",
        }

    # PDF (by magic bytes)
    if file_path and _is_pdf(file_path):
        return {
            "content_type": "pdf",
            "lane": "heavy",
            "route_reason": "PDF file detected by magic bytes",
            "stage": "route",
        }

    # Direct text content
    if content and content.strip():
        return {
            "content_type": "text",
            "lane": "instant",
            "route_reason": "direct text content provided",
            "stage": "route",
        }

    # Fallback: treat as url (content-core will attempt extraction)
    return {
        "content_type": "url",
        "lane": "instant",
        "route_reason": "no classifier matched, falling back to url extraction",
        "stage": "route",
    }


async def extract_stage(state: Source7StageState) -> dict:
    """Stage 3: Extract — run content-core extraction or native wrapper.

    Routes to native binary wrappers when available:
      - pdf → pdftotext (Day 1), falling back to tesseract OCR when the PDF
        is scanned (pdftotext yields < 50 chars/page).
      - spreadsheet (.xlsx/.xls/.ods) → ssconvert (Day 4).
      - csv → native UTF-8 text read (Day 4, no binary needed).
      - image → tesseract OCR (Day 4, heavy lane).
      - html/url → trafilatura (Day 3).
      - document (docx/odt/epub/rtf) → pandoc (Day 2).

    Conditional stage timeout: "heavy" lane (OCR/STT) gets 1800s so 75+ page
    scanned PDFs do not abort mid-OCR; "instant" lane stays at 300s.

    On failure, adds the error to state.errors and returns so the
    graph's compensation edge can route to compensate_extract.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=30, stage="extract")
    await raise_if_cancelled(cmd_id)

    errors = list(state.get("errors", []))
    content_state = state.get("content_state") or {}
    content_type = state.get("content_type", "url")
    lane = state.get("lane", "instant")
    file_path = content_state.get("file_path")
    url = content_state.get("url") or ""

    # Conditional stage timeout: heavy lane (OCR/STT) → 1800s, instant → 300s.
    effective_timeout = STAGE_TIMEOUT_HEAVY if lane == "heavy" else STAGE_TIMEOUT_EXTRACT

    try:
        result = await asyncio.wait_for(
            _dispatch_extract(
                state, cmd_id, errors, content_type, lane, file_path, content_state, url
            ),
            timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        errors.append(f"extract: timeout after {effective_timeout}s ({lane} lane)")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: wrapper error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return result


async def _dispatch_extract(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    content_type: str,
    lane: str,
    file_path: Optional[str],
    content_state: Dict[str, Any],
    url: str,
) -> dict:
    """Dispatch to the correct native wrapper based on content_type.

    Order matters: HTML/URL → trafilatura must come before pandoc so HTML
    files are not swallowed by pandoc. Spreadsheet and CSV are handled
    before the document route.
    """
    # PDF extraction via pdftotext wrapper (Day 1). Scanned PDFs (pdftotext
    # yields < 50 chars/page) fall back to tesseract OCR.
    if content_type == "pdf" and file_path:
        result = await _extract_pdf_stage(state, cmd_id, errors, file_path, content_state)
        extraction = result.get("extraction")
        if extraction and extraction.content and len(extraction.content.strip()) >= 50:
            return result
        # pdftotext returned little/no text → scanned PDF, use tesseract OCR.
        page_count = content_state.get("page_count") or 1
        language = content_state.get("language") or ""
        ocr_result = await extract_ocr(
            command_id=cmd_id or "",
            file_path=file_path,
            page_count=int(page_count),
            language=language,
        )
        return _ocr_result_to_extract_dict(
            state, cmd_id, errors, ocr_result, content_state, "pdf"
        )

    # Spreadsheet conversion via ssconvert (Day 4).
    if content_type == "spreadsheet" and file_path:
        return await _extract_ssconvert_stage(state, cmd_id, errors, file_path, content_state)

    # CSV native text read (Day 4 — no binary, bypasses ssconvert).
    if content_type == "csv" and file_path:
        return await _extract_csv_stage(state, cmd_id, errors, file_path, content_state)

    # Image OCR via tesseract (Day 4, heavy lane).
    if content_type == "image" and file_path:
        page_count = 1
        language = content_state.get("language") or ""
        ocr_result = await extract_ocr(
            command_id=cmd_id or "",
            file_path=file_path,
            page_count=page_count,
            language=language,
        )
        return _ocr_result_to_extract_dict(
            state, cmd_id, errors, ocr_result, content_state, "image"
        )

    # HTML extraction via trafilatura wrapper (Day 3).
    # Must come BEFORE pandoc so HTML files don't get swallowed by pandoc.
    if content_type in ("url", "html") and (file_path or url):
        return await _extract_trafilatura_stage(state, cmd_id, errors, file_path, content_state, url)

    # Document conversion via pandoc wrapper (docx, odt, epub, rtf).
    if content_type == "document" and file_path:
        return await _extract_pandoc_stage(state, cmd_id, errors, file_path, content_state)

    # Fall back to content-core for URLs, text, media, and unknown types.
    return await _extract_content_core_stage(state, cmd_id, errors, content_state, url)


def _ocr_result_to_extract_dict(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    ocr_result: Dict[str, Any],
    content_state: Dict[str, Any],
    source_kind: str,
) -> dict:
    """Convert an OCR ExtractResult dict into an extract_stage result dict.

    Low confidence (< 60% mean) sets extracting_partial via a warning but
    does not fail — partial OCR text is still useful content.
    """
    extraction = _ExtractionOutput(
        content=ocr_result.get("text", ""),
        title=content_state.get("title", ""),
    )

    # Propagate OCR warnings (low confidence, timed-out pages).
    for warning in ocr_result.get("warnings", []):
        errors.append(f"extract: {warning}")

    timed_out = ocr_result.get("timed_out_pages", [])
    if timed_out:
        errors.append(
            f"extract: pages {sorted(timed_out)} timed out during {source_kind} OCR"
        )

    if ocr_result.get("low_confidence"):
        errors.append(
            "extract: low_ocr_confidence — text may be inaccurate "
            "(extracting_partial)"
        )

    if not extraction.content.strip():
        errors.append(f"extract: {source_kind} OCR returned no text content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_pdf_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    file_path: str,
    content_state: Dict[str, Any],
) -> dict:
    """PDF extraction via pdftotext wrapper with per-range timeout + checkpoint."""
    from open_notebook.preprocess.tools.pdf import extract_pdf

    # Get page count from content_state or estimate
    page_count = content_state.get("page_count") or 1
    language = content_state.get("language") or ""

    try:
        result = await extract_pdf(
            command_id=cmd_id or "",
            file_path=file_path,
            page_count=int(page_count),
            language=language,
        )
    except asyncio.TimeoutError:
        errors.append(f"extract: pdf timeout after {STAGE_TIMEOUT_EXTRACT}s")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: pdf wrapper error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    extraction = _ExtractionOutput(
        content=result.get("text", ""),
        title=content_state.get("title", ""),
    )

    # Record timed-out pages as warnings so the user knows content was missed.
    timed_out = result.get("timed_out_pages", [])
    if timed_out:
        errors.append(
            f"extract: pages {sorted(timed_out)} timed out during extraction"
        )

    if not extraction.content.strip():
        errors.append("extract: pdf returned no text content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_ssconvert_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    file_path: str,
    content_state: Dict[str, Any],
) -> dict:
    """Spreadsheet conversion via ssconvert wrapper (xlsx, xls, ods).

    Converts to CSV then formats as a Markdown table. Missing binary or
    non-zero exit raises ValueError which routes to compensation.
    """
    try:
        result = await convert_spreadsheet(
            command_id=cmd_id or "",
            file_path=file_path,
        )
    except ValueError as exc:  # ssconvert_failed → compensation
        errors.append(f"extract: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except asyncio.TimeoutError:
        errors.append(f"extract: ssconvert timeout after {STAGE_TIMEOUT_EXTRACT}s")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: ssconvert wrapper error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    if result.get("truncated"):
        errors.append(
            f"extract: spreadsheet truncated at {MAX_TABLE_ROWS} rows "
            "(use a smaller sheet or split into multiple files)"
        )

    extraction = _ExtractionOutput(
        content=result.get("text", ""),
        title=content_state.get("title", ""),
    )

    if not extraction.content.strip():
        errors.append("extract: ssconvert returned no table content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_csv_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    file_path: str,
    content_state: Dict[str, Any],
) -> dict:
    """CSV native text read (bypasses ssconvert — no binary needed)."""
    try:
        result = await read_csv_native(
            command_id=cmd_id or "",
            file_path=file_path,
        )
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: csv read error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    extraction = _ExtractionOutput(
        content=result.get("text", ""),
        title=content_state.get("title", ""),
    )

    if not extraction.content.strip():
        errors.append("extract: csv returned no text content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_trafilatura_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    file_path: Optional[str],
    content_state: Dict[str, Any],
    url: str,
) -> dict:
    """HTML extraction via trafilatura wrapper (HTML/URL content).

    Routes to trafilatura for HTML/web content. trafilatura strips boilerplate
    automatically and detects JavaScript-required (SPA) pages. On empty/blocked
    HTML it raises ValueError which routes to compensate_extract.
    """
    from open_notebook.preprocess.tools.trafilatura import extract_html

    try:
        result = await extract_html(
            command_id=cmd_id or "",
            file_path=file_path,
            html_content=content_state.get("content"),
            url=url or content_state.get("url") or "",
        )
    except asyncio.TimeoutError:
        errors.append(f"extract: trafilatura timeout after {STAGE_TIMEOUT_EXTRACT}s")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except ValueError as exc:  # html_extraction_empty → compensation
        errors.append(f"extract: trafilatura empty/blocked HTML: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: trafilatura wrapper error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    # Build ExtractionOutput-compatible result
    extraction = _ExtractionOutput(
        content=result.get("text", ""),
        title=content_state.get("title", ""),
    )

    # Record JavaScript-required as a warning (extracting_partial, not failed)
    if result.get("javascript_required"):
        errors.append(
            "extract: trafilatura javascript_required — page may need JavaScript to render"
        )

    # Record timed-out pages
    timed_out = result.get("timed_out_pages", [])
    if timed_out:
        errors.append(
            f"extract: pages {sorted(timed_out)} timed out during HTML extraction"
        )

    if not extraction.content.strip():
        errors.append("extract: trafilatura returned no text content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_pandoc_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    file_path: str,
    content_state: Dict[str, Any],
) -> dict:
    """Document conversion via pandoc wrapper (docx, odt, epub, rtf, html)."""
    from open_notebook.preprocess.tools.pandoc import convert_document

    try:
        result = await convert_document(
            command_id=cmd_id or "",
            file_path=file_path,
            source_format=content_state.get("source_format", "auto"),
            language=content_state.get("language", ""),
        )
    except asyncio.TimeoutError:
        errors.append(f"extract: pandoc timeout after {STAGE_TIMEOUT_EXTRACT}s")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except FileNotFoundError:
        errors.append("extract: pandoc binary not installed")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: pandoc wrapper error: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    extraction = _ExtractionOutput(
        content=result.get("text", ""),
        title=content_state.get("title", ""),
    )

    if not extraction.content.strip():
        errors.append("extract: pandoc returned no text content")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def _extract_content_core_stage(
    state: Source7StageState,
    cmd_id: Optional[str],
    errors: List[str],
    content_state: Dict[str, Any],
    url: str,
) -> dict:
    """Fallback to content-core extraction for URLs, text, media, unknown types."""
    from typing import cast

    # Reuse the existing content_process node logic via a compatibility
    # shim: build a SourceState-compatible dict and call content_process.
    compat_state = {
        "content_state": content_state,
        "source_id": state.get("source_id", ""),
        "notebook_ids": state.get("notebook_ids", []),
        "apply_transformations": state.get("apply_transformations", []),
        "embed": state.get("embed", False),
        "source": state.get("source"),
        "transformation": [],
    }

    try:
        result = await asyncio.wait_for(
            content_process(cast(SourceState, compat_state)),
            timeout=STAGE_TIMEOUT_EXTRACT,
        )
    except asyncio.TimeoutError:
        errors.append(f"extract: timeout after {STAGE_TIMEOUT_EXTRACT}s")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"extract: {exc}")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    extraction = result.get("extraction")

    if not extraction:
        errors.append("extract: content-core returned no result")
        return {
            "extraction": None,
            "normalized_text": "",
            "errors": errors,
            "stage": "extract",
        }

    return {
        "extraction": extraction,
        "errors": errors,
        "stage": "extract",
    }


async def normalize_stage(state: Source7StageState) -> dict:
    """Stage 4: Normalize — clean and validate extracted text.

    Handles:
      - Empty content → actionable error (YouTube no-transcript, scanned PDF)
      - Scanned PDF without OCR → fail fast with _SCANNED_PDF_MESSAGE
      - Media files where both extractor and STT failed → _media_failure_message

    On failure, adds the error to state.errors and returns so the
    graph's compensation edge can route to compensate_normalize.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=45, stage="normalize")
    await raise_if_cancelled(cmd_id)

    errors = list(state.get("errors", []))

    extraction = state.get("extraction")
    content_state = state.get("content_state") or {}
    content_type = state.get("content_type", "url")
    file_path = content_state.get("file_path")
    url = content_state.get("url") or ""

    if not extraction or not extraction.content or not extraction.content.strip():
        if url and _is_youtube_url(url):
            errors.append("normalize: " + _YOUTUBE_NO_TRANSCRIPT_MESSAGE)
            return {
                "normalized_text": "",
                "errors": errors,
                "stage": "normalize",
            }
        if content_type == "media":
            attempted = bool(content_state.get("_stt_attempted"))
            errors.append("normalize: " + _media_failure_message(file_path or "", attempted=attempted))
            return {
                "normalized_text": "",
                "errors": errors,
                "stage": "normalize",
            }
        if file_path and _is_pdf(file_path):
            errors.append("normalize: " + _SCANNED_PDF_MESSAGE)
            return {
                "normalized_text": "",
                "errors": errors,
                "stage": "normalize",
            }
        errors.append(
            "normalize: "
            + (
                _empty_file_message(file_path, content_state)
                if file_path
                else "Could not extract any text content from this source."
            )
        )
        return {
            "normalized_text": "",
            "errors": errors,
            "stage": "normalize",
        }

    return {
        "normalized_text": extraction.content.strip(),
        "errors": errors,
        "stage": "normalize",
    }


async def chunk_stage(state: Source7StageState) -> dict:
    """Stage 5: Chunk — split normalized text into embedding-ready chunks.

    Uses chunk_text_with_coords for content-aware splitting with
    character coordinates. Drops sub-minimum chunks. Stores chunks and
    chunk_infos for the embed stage.

    On failure, adds the error to state.errors and returns so the
    graph's compensation edge can route to compensate_chunk.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=60, stage="chunk")
    await raise_if_cancelled(cmd_id)

    errors = list(state.get("errors", []))

    normalized_text = state.get("normalized_text") or ""
    if not normalized_text.strip():
        return {
            "chunks": [],
            "chunk_infos": [],
            "errors": errors,
            "stage": "chunk",
        }

    content_state = state.get("content_state") or {}
    file_path = content_state.get("file_path")

    try:
        chunk_infos = await asyncio.wait_for(
            asyncio.to_thread(chunk_text_with_coords, normalized_text, file_path=file_path),
            timeout=STAGE_TIMEOUT_CHUNK,
        )
    except asyncio.TimeoutError:
        errors.append(f"chunk: timeout after {STAGE_TIMEOUT_CHUNK}s")
        return {
            "chunks": [],
            "chunk_infos": [],
            "errors": errors,
            "stage": "chunk",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"chunk: {exc}")
        return {
            "chunks": [],
            "chunk_infos": [],
            "errors": errors,
            "stage": "chunk",
        }

    chunks = [ci["text"] for ci in chunk_infos]

    # Checkpoint every CHECKPOINT_CHUNK_EVERY chunks for progress visibility.
    total_chunks = len(chunks)
    if total_chunks > 0 and total_chunks % CHECKPOINT_CHUNK_EVERY == 0:
        await report_progress_record(
            cmd_id,
            progress=60 + min(20, total_chunks // CHECKPOINT_CHUNK_EVERY * 2),
            stage="chunk",
        )

    return {
        "chunks": chunks,
        "chunk_infos": chunk_infos,
        "errors": errors,
        "stage": "chunk",
    }


async def embed_stage(state: Source7StageState) -> dict:
    """Stage 6: Embed — generate embeddings for all chunks.

    Batches chunks through generate_embeddings (with retry). Does NOT
    store to DB — that's the index stage. Skipped when embed=False.

    On failure, adds the error to state.errors and returns so the
    graph's compensation edge can route to compensate_embed.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=75, stage="embed")
    await raise_if_cancelled(cmd_id)

    errors = list(state.get("errors", []))

    if not state.get("embed", False):
        return {
            "embeddings": [],
            "embedded_count": 0,
            "skipped": ["embed"],
            "errors": errors,
            "stage": "embed",
        }

    chunks = state.get("chunks") or []
    if not chunks:
        return {
            "embeddings": [],
            "embedded_count": 0,
            "errors": errors,
            "stage": "embed",
        }

    content_state = state.get("content_state") or {}
    file_path = content_state.get("file_path")

    from open_notebook.utils.chunking import detect_content_type
    from open_notebook.utils.embedding import generate_embeddings

    content_type = detect_content_type(
        state.get("normalized_text") or "", file_path=file_path
    )

    try:
        embeddings = await asyncio.wait_for(
            generate_embeddings(
                chunks,
                command_id=cmd_id,
            ),
            timeout=STAGE_TIMEOUT_EMBED,
        )
    except asyncio.TimeoutError:
        errors.append(f"embed: timeout after {STAGE_TIMEOUT_EMBED}s")
        return {
            "embeddings": [],
            "embedded_count": 0,
            "errors": errors,
            "stage": "embed",
        }
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"embed: {exc}")
        return {
            "embeddings": [],
            "embedded_count": 0,
            "errors": errors,
            "stage": "embed",
        }

    # Checkpoint every CHECKPOINT_EMBED_EVERY embeddings for progress visibility.
    total_embeddings = len(embeddings)
    if total_embeddings > 0 and total_embeddings % CHECKPOINT_EMBED_EVERY == 0:
        await report_progress_record(
            cmd_id,
            progress=75 + min(15, total_embeddings // CHECKPOINT_EMBED_EVERY),
            stage="embed",
        )

    return {
        "embeddings": embeddings,
        "embedded_count": total_embeddings,
        "errors": errors,
        "stage": "embed",
    }


async def index_stage(state: Source7StageState) -> dict:
    """Stage 7: Index — persist everything to the database.

    - Updates the Source record with full_text (normalized_text)
    - Deletes and re-inserts source_chunk + source_embedding records
    - Runs apply_transformations (insight generation)
    - Returns final outcome

    On failure, adds the error to state.errors and returns so the
    graph's compensation edge can route to compensate_index.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=90, stage="index")
    await raise_if_cancelled(cmd_id)

    errors = list(state.get("errors", []))

    source = state.get("source")
    normalized_text = state.get("normalized_text") or ""
    embeddings = state.get("embeddings") or []
    chunk_infos = state.get("chunk_infos") or []
    apply_transformations = state.get("apply_transformations") or []

    if not source:
        errors.append("index: no source to index")
        return {
            "source": source,
            "errors": errors,
            "stage": "index",
        }

    # Update source full_text and save
    source.full_text = normalized_text
    previous = source.asset or Asset()
    source.asset = Asset(
        url=state.get("content_state", {}).get("url"),
        file_path=state.get("content_state", {}).get("file_path"),
        sha256=previous.sha256,
        filename=state.get("content_state", {}).get("filename") or previous.filename,
        size=previous.size,
    )
    # Set source status based on extraction errors: partial if any pages timed out.
    if any("timed out" in e for e in errors):
        source.status = "extracting_partial"
    else:
        source.status = "ready"
    try:
        await source.save()
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"index: save source failed: {exc}")
        return {
            "source": source,
            "errors": errors,
            "stage": "index",
        }

    # Delete existing chunks + embeddings (idempotency)
    source_id_rid = ensure_record_id(str(source.id))
    try:
        await repo_query(
            "DELETE source_embedding WHERE source = $source_id",
            {"source_id": source_id_rid},
        )
        await repo_query(
            "DELETE source_chunk WHERE source_id = $source_id",
            {"source_id": source_id_rid},
        )
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"index: delete old chunks failed: {exc}")
        return {
            "source": source,
            "errors": errors,
            "stage": "index",
        }

    # Insert chunk + embedding records
    from open_notebook.database.repository import repo_insert

    chunk_records = []
    embedding_records = []
    for idx, (chunk_info, embedding) in enumerate(
        zip(chunk_infos, embeddings)
    ):
        chunk = chunk_info["text"]
        text_hash = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16]

        chunk_records.append(
            {
                "source_id": source_id_rid,
                "text": chunk,
                "chunk_index": idx,
                "char_start": chunk_info["char_start"],
                "char_end": chunk_info["char_end"],
                "page_number": 1,  # TODO: compute from form-feed boundaries
                "heading_path": None,
                "index_generation": 1,
                "text_hash": text_hash,
                "embedding": embedding if idx < len(embeddings) else None,
            }
        )

    try:
        if chunk_records:
            await repo_insert("source_chunk", chunk_records)
    except Exception as exc:  # noqa: BLE001 - capture for compensation
        errors.append(f"index: insert chunks failed: {exc}")
        return {
            "source": source,
            "errors": errors,
            "stage": "index",
        }

    # Run transformations (insights)
    insights_created = 0
    for transformation in apply_transformations:
        if not normalized_text.strip():
            continue
        try:
            result = await asyncio.wait_for(
                transform_graph.ainvoke(  # type: ignore[call-overload]
                    dict(input_text=normalized_text, transformation=transformation),
                    config=RunnableConfig(configurable={"model_id": transformation.model_id}),
                ),
                timeout=STAGE_TIMEOUT_INDEX,
            )
            await source.add_insight(transformation.title, result["output"])
            insights_created += 1
        except asyncio.TimeoutError:
            errors.append(f"index: transformation {transformation.name} timeout after {STAGE_TIMEOUT_INDEX}s")
            continue
        except Exception as exc:  # noqa: BLE001 - capture for compensation
            errors.append(f"index: transformation {transformation.name} failed: {exc}")

    await report_progress_record(cmd_id, progress=100, stage="completed")

    return {
        "source": source,
        "embedded_count": len(chunk_records),
        "insights_created": insights_created,
        "errors": errors,
        "stage": "index",
    }


# =====================================================================
# Compensation handlers (Week 2 poly-pipeline core value)
#
# Each stage 3-7 has a compensation handler that cleans up its own partial
# output on failure, so a retry doesn't orphan chunks/embeddings or silently
# corrupt RAG. This is the fix for the audit's HIGH-severity defect:
# "source_graph nodes have no compensation/rollback."
# =====================================================================

async def compensate_extract(state: Source7StageState) -> dict:
    """Compensate for a failed extraction.

    Clears any partial extraction result so the source can be retried
    from a clean state rather than leaving orphaned content.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=30, stage="extract_compensate")
    await raise_if_cancelled(cmd_id)

    return {
        "extraction": None,
        "normalized_text": "",
        "stage": "extract_compensated",
        "errors": state.get("errors", [])
        + ["extraction failed, compensated and ready for retry"],
    }


async def compensate_normalize(state: Source7StageState) -> dict:
    """Compensate for a failed normalization.

    Drops the partial normalized text so the extract stage can be retried.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=45, stage="normalize_compensate")
    await raise_if_cancelled(cmd_id)

    return {
        "normalized_text": "",
        "stage": "normalize_compensated",
        "errors": state.get("errors", [])
        + ["normalization failed, compensated and ready for retry"],
    }


async def compensate_chunk(state: Source7StageState) -> dict:
    """Compensate for a failed chunking.

    Drops any partial chunks so the normalize stage can be retried.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=60, stage="chunk_compensate")
    await raise_if_cancelled(cmd_id)

    return {
        "chunks": [],
        "chunk_infos": [],
        "stage": "chunk_compensated",
        "errors": state.get("errors", [])
        + ["chunking failed, compensated and ready for retry"],
    }


async def compensate_embed(state: Source7StageState) -> dict:
    """Compensate for a failed embedding.

    Drops partial embeddings so chunks can be re-embedded on retry.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=75, stage="embed_compensate")
    await raise_if_cancelled(cmd_id)

    return {
        "embeddings": [],
        "embedded_count": 0,
        "stage": "embed_compensated",
        "errors": state.get("errors", [])
        + ["embedding failed, compensated and ready for retry"],
    }


async def compensate_index(state: Source7StageState) -> dict:
    """Compensate for a failed index.

    Deletes any partial chunk/embedding records written to the DB so the
    source can be re-indexed cleanly.
    """
    cmd_id = get_current_command_id()
    await report_progress_record(cmd_id, progress=90, stage="index_compensate")
    await raise_if_cancelled(cmd_id)

    source = state.get("source")
    if source:
        source_id_rid = ensure_record_id(str(source.id))
        try:
            await repo_query(
                "DELETE source_embedding WHERE source = $source_id",
                {"source_id": source_id_rid},
            )
            await repo_query(
                "DELETE source_chunk WHERE source_id = $source_id",
                {"source_id": source_id_rid},
            )
        except Exception as exc:  # noqa: BLE001 - compensation must not mask the original error
            logger.warning(f"compensation cleanup failed for {source.id}: {exc}")

    return {
        "stage": "index_compensated",
        "errors": state.get("errors", [])
        + ["indexing failed, compensated and ready for retry"],
    }


# =====================================================================
# 7-stage graph definition with conditional edges and compensation
# =====================================================================

workflow_7stage = StateGraph(Source7StageState)

workflow_7stage.add_node("ingest", ingest_stage)
workflow_7stage.add_node("route", route_stage)
workflow_7stage.add_node("extract_instant", extract_stage)
workflow_7stage.add_node("extract_heavy", extract_stage)
workflow_7stage.add_node("normalize", normalize_stage)
workflow_7stage.add_node("chunk", chunk_stage)
workflow_7stage.add_node("embed", embed_stage)
workflow_7stage.add_node("index", index_stage)
# Compensation handlers
workflow_7stage.add_node("compensate_extract", compensate_extract)
workflow_7stage.add_node("compensate_normalize", compensate_normalize)
workflow_7stage.add_node("compensate_chunk", compensate_chunk)
workflow_7stage.add_node("compensate_embed", compensate_embed)
workflow_7stage.add_node("compensate_index", compensate_index)

workflow_7stage.add_edge(START, "ingest")
workflow_7stage.add_edge("ingest", "route")

# Conditional edge from route: lane determines extraction path
workflow_7stage.add_conditional_edges(
    "route",
    lambda state: state.get("content_type", "url"),
    {
        "media": "extract_heavy",      # audio/video → STT path
        "pdf": "extract_heavy",        # scanned PDF → OCR path
        "image": "extract_heavy",      # image → tesseract OCR path
        "url": "extract_instant",      # URL → direct extraction
        "html": "extract_instant",     # HTML → trafilatura (instant lane)
        "text": "extract_instant",     # direct text → instant
        "spreadsheet": "extract_instant",  # xlsx/xls/ods → ssconvert (fast)
        "csv": "extract_instant",      # CSV → native text read (fast)
        "deferred": "extract_heavy",   # deferred lane → heavy extraction
    },
)

# Each extract stage either succeeds (→ normalize) or fails (→ compensate)
workflow_7stage.add_edge("extract_instant", "normalize")
workflow_7stage.add_edge("extract_heavy", "normalize")
workflow_7stage.add_edge("normalize", "chunk")
workflow_7stage.add_edge("chunk", "embed")
workflow_7stage.add_edge("embed", "index")
workflow_7stage.add_edge("index", END)

# Compensation edges — on error, route to the matching compensator
workflow_7stage.add_conditional_edges(
    "extract_instant",
    lambda state: "compensate" if state.get("errors") else "normalize",
    {"compensate": "compensate_extract", "normalize": "normalize"},
)
workflow_7stage.add_conditional_edges(
    "extract_heavy",
    lambda state: "compensate" if state.get("errors") else "normalize",
    {"compensate": "compensate_extract", "normalize": "normalize"},
)
workflow_7stage.add_conditional_edges(
    "normalize",
    lambda state: "compensate" if state.get("errors") else "chunk",
    {"compensate": "compensate_normalize", "chunk": "chunk"},
)
workflow_7stage.add_conditional_edges(
    "chunk",
    lambda state: "compensate" if state.get("errors") else "embed",
    {"compensate": "compensate_chunk", "embed": "embed"},
)
workflow_7stage.add_conditional_edges(
    "embed",
    lambda state: "compensate" if state.get("errors") else "index",
    {"compensate": "compensate_embed", "index": "index"},
)
workflow_7stage.add_conditional_edges(
    "index",
    lambda state: "compensate" if state.get("errors") else END,
    {"compensate": "compensate_index", END: END},
)

source_graph = workflow_7stage.compile()
