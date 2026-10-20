import asyncio
import operator
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
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


async def _try_stt(
    file_path: str, cmd_id: str | None = None, language: str = ""
) -> str:
    """Transcribe a media file with the local speech server.

    Returns empty string rather than raising, for the same reason `_try_ocr`
    does: speech-to-text being unavailable or refusing the file is a degraded
    outcome, and the caller's fallback is the actionable error. Raising here
    would turn "this phone has no whisper build" into a job that fails fifteen
    times with exponential backoff and starves the queue behind it.
    """
    from open_notebook.speech import transcribe

    if cmd_id:
        await report_progress_record(cmd_id, progress=20, stage="transcribing")
    # The declared language, when there is one. It reaches both the model and
    # the residency gate, because the tier follows from it: Mandarin is wrong
    # at CER 0.222 on the small model and exact on the large one, for five
    # times the time. Without a declared language the model guesses, and on
    # Tamil and Malay it guesses wrong and emits mixed-script noise.
    result = await transcribe(file_path, language=language or None)
    if not result.ok:
        if result.held:
            # A residency hold means the model is loaded but the device is
            # committed. Say so plainly in the log and let the honest failure
            # message reach the operator.
            logger.info(
                f"speech-to-text held for {Path(file_path).name}: {result.error}"
            )
        else:
            logger.warning(
                f"speech-to-text failed for {Path(file_path).name}: {result.error}"
            )
        return ""
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


# Create and compile the workflow
workflow = StateGraph(SourceState)

# Add nodes
workflow.add_node("content_process", content_process)
workflow.add_node("save_source", save_source)
workflow.add_node("transform_content", transform_content)
# Define the graph edges
workflow.add_edge(START, "content_process")
workflow.add_edge("content_process", "save_source")
workflow.add_conditional_edges(
    "save_source", trigger_transformations, ["transform_content"]
)
workflow.add_edge("transform_content", END)

# Compile the graph
source_graph = workflow.compile()
