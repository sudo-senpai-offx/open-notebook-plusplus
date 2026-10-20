"""Speech in and speech out, against servers that live outside the guest.

Why this module exists
----------------------
The phone runs whisper.cpp (STT) and piper (TTS) as two more Termux-resident
model servers, alongside chat and embed. Proot cannot signal or read them, so
like the others they are reached only over loopback and their residency is
decided by `termux-supervisor.py` — which is why this module *asks* before it
calls, rather than starting anything itself.

What is deliberately not here
-----------------------------
A fallback to "assume it is loaded" when the supervisor is unreachable. That is
the exact failure the residency design exists to prevent: a job that runs
against nothing and reports success. Unavailable is a value the caller holds or
degrades on, never a shrug.

Both servers speak the OpenAI shapes — `/v1/audio/transcriptions` and
`/v1/audio/speech` — so the same call serves the PC's docker-compose whisper and
the phone's Termux build with no branching on which one answered.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

#: Termux ports. On the phone the guest shares Termux's network namespace, so
#: these are the real addresses; on the PC the same paths are reached through the
#: tunnel's forwarded ports, which is why both are overridable rather than
#: compiled in.
DEFAULT_STT_URL = "http://127.0.0.1:8090"
DEFAULT_TTS_URL = "http://127.0.0.1:8091"

#: A quick availability probe must not hang: it runs on the request path of the
#: status panel and of every source extraction.
DEFAULT_PROBE_TIMEOUT = 3.0

#: Transcription is slow, and slow *honestly*: an hour of lecture audio on four
#: Cortex-A53s runs well past real time. The default is generous on purpose —
#: a timeout here surfaces as "this file could not be transcribed", which an
#: operator will read as a broken feature rather than as a device being busy.
DEFAULT_STT_TIMEOUT = 1800.0

#: Synthesis is bounded by the length of the text and is not doing competitive
#: arithmetic on a 25 ms frame, so minutes rather than half an hour.
DEFAULT_TTS_TIMEOUT = 600.0

#: Filename suffix whisper.cpp is told about. It sniffs content itself, but an
#: explicit name lets it pick the right decoder instead of guessing.
_TRANSCRIBE_SUFFIX = ".wav"


def stt_url() -> str:
    return (
        os.getenv("OPEN_NOTEBOOK_STT_URL", DEFAULT_STT_URL) or DEFAULT_STT_URL
    ).rstrip("/")


def tts_url() -> str:
    return (
        os.getenv("OPEN_NOTEBOOK_TTS_URL", DEFAULT_TTS_URL) or DEFAULT_TTS_URL
    ).rstrip("/")


def _timeout(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning(f"{env_name}={raw!r} is not a number; using {default}")
    return default


@dataclass(frozen=True)
class TranscriptionResult:
    ok: bool
    text: str = ""
    #: Duration the server reported, in seconds. Zero when it reported none.
    seconds: float = 0.0
    error: Optional[str] = None
    #: True when the failure was a residency refusal rather than a transcription
    #: failure. The distinction matters to the caller: a hold is retryable and
    #: costs nothing, a transcription error is worth surfacing to the operator.
    held: bool = False


@dataclass(frozen=True)
class SynthesisResult:
    ok: bool
    path: Optional[Path] = None
    bytes_written: int = 0
    error: Optional[str] = None
    held: bool = False


async def _ensure(role: str, variant: str = "") -> Optional[str]:
    """Ask the supervisor for `role`; return an error string, or None if ready.

    `variant` selects which model the role loads, when it has more than one.
    For STT it is the language of the job, and the tier follows from it:
    measured on the device, Mandarin at the small tier scores CER 0.222 -
    fluent Mandarin that is not what was said - against CER 0.000 at the large
    tier, for five times the time.

    Returns early when residency control is off, which is the PC's normal state
    and the one in which these servers are started by hand and simply reachable.
    """
    from open_notebook.residency import client as residency

    # Full residency: the supervisor owns every role.
    if residency.enabled():
        result = await residency.ensure_resident(role, variant)
        if result.ok:
            return None
        return result.hold_reason()

    # Model selection only: the app may ask the phone for a *particular* model
    # without the whole residency regime switching on. This is what the
    # per-language speech tier needs - Mandarin has to be able to reach the
    # 547 MB multilingual model while English stays on the 181 MB one.
    #
    # Opportunistic, and deliberately so: a device with no supervisor still
    # transcribes, just with whatever model is loaded. The language still
    # reaches the request either way, which is what stops the model
    # transliterating Tamil into Latin script and scoring CER 1.0 against a
    # Tamil-script reference.
    if variant and residency.model_selection_enabled():
        result = await residency.select_model(role, variant)
        if not result.ok:
            # A supervisor that refuses or is unreachable must not fail the
            # job: the transcript would still be produced, just at a tier that
            # may be wrong for the language. Holding the job for it would
            # stall the queue behind a device-configuration problem.
            logger.warning(
                "residency could not select the model for %s (%s): %s",
                role,
                variant,
                result.error,
            )
    return None


async def available(role: str = "stt") -> tuple[bool, str]:
    """Is this speech server up right now?

    Same `(ok, detail)` shape as `ocr.available()`, because callers use it the
    same way: a boolean to branch on and a sentence to show when it is False.

    Deliberately probes the server's own liveness endpoint rather than assuming
    a successful residency call means the process is serving. `ensure_resident`
    can return `ok=True` for a role it has only just started and not yet seen
    answer.
    """
    url = stt_url() if role == "stt" else tts_url()
    probe_path = "/health" if role == "stt" else "/v1/models"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=DEFAULT_PROBE_TIMEOUT) as client:
            response = await client.get(f"{url}{probe_path}")
    except Exception as exc:  # noqa: BLE001 - absent server is a value
        return False, f"{role} server not reachable at {url} ({type(exc).__name__})"
    if response.status_code != 200:
        return False, f"{role} server at {url} answered {response.status_code}"
    return True, f"{role} ready at {url}"


async def transcribe(
    audio_path: str | Path,
    *,
    language: Optional[str] = None,
    ensure_resident: bool = True,
) -> TranscriptionResult:
    """Transcribe an audio or video file to plain text.

    Both whisper.cpp and the OpenAI-compatible endpoint take a multipart upload
    and answer `{"text": ...}`. Anything else — a 404 because the server was
    started without `--inference-path`, a 500 because the file is not audio — is
    reported as an error rather than as empty text, because an empty transcript
    and a failed transcript are different facts and the caller must not conflate
    them.
    """
    path = Path(audio_path)
    if not path.is_file():
        return TranscriptionResult(ok=False, error=f"no such file: {path}")

    if ensure_resident:
        held = await _ensure("stt", language or "")
        if held is not None:
            return TranscriptionResult(ok=False, error=held, held=True)

    try:
        import httpx

        data = {"response_format": "json"}
        if language:
            data["language"] = language
        async with httpx.AsyncClient(
            timeout=_timeout("OPEN_NOTEBOOK_STT_TIMEOUT", DEFAULT_STT_TIMEOUT)
        ) as client:
            with path.open("rb") as handle:
                response = await client.post(
                    f"{stt_url()}/v1/audio/transcriptions",
                    files={
                        "file": (path.name + _TRANSCRIBE_SUFFIX, handle, "audio/wav")
                    },
                    data=data,
                )
    except Exception as exc:  # noqa: BLE001 - transport failure is a value
        logger.warning(f"transcription request failed for {path}: {exc}")
        return TranscriptionResult(
            ok=False, error=f"transcription request failed: {exc}"
        )

    if response.status_code != 200:
        # The body can be long HTML from a proxy in front of the server; one
        # line is enough to identify which failure this was.
        first = response.text.strip().splitlines()[:1]
        detail = first[0][:200] if first else ""
        return TranscriptionResult(
            ok=False,
            error=f"stt server answered {response.status_code}"
            + (f": {detail}" if detail else ""),
        )

    try:
        payload = response.json()
    except ValueError:
        return TranscriptionResult(ok=False, error="stt server sent non-JSON")

    text = str(payload.get("text") or "").strip()
    if not text:
        # A successful response with no text is a *failure to transcribe*, not
        # a silent audio file, and the caller has to be able to tell them apart:
        # silence gets a different message from "the model produced nothing".
        return TranscriptionResult(
            ok=False, error="stt server returned an empty transcript"
        )
    return TranscriptionResult(ok=True, text=text)


async def synthesize(
    text: str,
    out_path: str | Path,
    *,
    voice: Optional[str] = None,
    ensure_resident: bool = True,
) -> SynthesisResult:
    """Render `text` to an audio file at `out_path`.

    piper answers raw bytes for the OpenAI path, so the response body *is* the
    file. Written to a temporary name and renamed on success, because a partial
    write that fails mid-stream would otherwise leave a truncated file at a path
    a podcast episode already points at.
    """
    body = (text or "").strip()
    if not body:
        return SynthesisResult(ok=False, error="nothing to synthesize")

    if ensure_resident:
        held = await _ensure("tts")
        if held is not None:
            return SynthesisResult(ok=False, error=held, held=True)

    destination = Path(out_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SynthesisResult(ok=False, error=f"cannot write to {destination}: {exc}")

    payload: dict[str, str] = {"input": body, "response_format": "wav"}
    if voice:
        payload["voice"] = voice

    temporary = destination.with_name(destination.name + ".part")
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=_timeout("OPEN_NOTEBOOK_TTS_TIMEOUT", DEFAULT_TTS_TIMEOUT)
        ) as client:
            response = await client.post(
                f"{tts_url()}/v1/audio/speech",
                json=payload,
                headers={"Accept": "audio/wav"},
            )
        if response.status_code != 200:
            first = response.text.strip().splitlines()[:1]
            detail = first[0][:200] if first else ""
            return SynthesisResult(
                ok=False,
                error=f"tts server answered {response.status_code}"
                + (f": {detail}" if detail else ""),
            )
        temporary.write_bytes(response.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"synthesis request failed for {destination}: {exc}")
        _unlink_quietly(temporary)
        return SynthesisResult(ok=False, error=f"synthesis request failed: {exc}")

    if not temporary.is_file() or temporary.stat().st_size == 0:
        _unlink_quietly(temporary)
        return SynthesisResult(ok=False, error="tts server returned no audio")

    try:
        temporary.replace(destination)
    except OSError as exc:
        _unlink_quietly(temporary)
        return SynthesisResult(ok=False, error=f"cannot finalise {destination}: {exc}")

    return SynthesisResult(
        ok=True, path=destination, bytes_written=destination.stat().st_size
    )


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
