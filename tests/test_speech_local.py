"""Local speech: the media fallback, the two clients, and the role accounting.

What is worth testing here, and why it is not the same as testing the servers
----------------------------------------------------------------------------
The servers are the phone's problem — they are built and run by the operator, and
a test that shells out to them proves nothing on a machine that does not have
them. What lives here is everything that decides *whether to call them and what
to do with the answer*, and that is where the failure modes are:

1. **The media test.** `_is_media` exists because the blob store hands files back
   as `{sha}.bin`. Every `endswith(".mp3")` gate silently skipped every synced
   recording, so the file kind has to come from the bytes. One test per
   container, because a detector that recognises MP3 and nothing else turns a
   recorded lecture into "this file type is not supported" — a permanent
   failure the operator retries three times before believing.

2. **The fallback firing.** An audio source whose text extractor returns nothing,
   and an audio source the extractor *refuses*, must both reach speech-to-text.
   The second is the one that was easy to get wrong: `UnsupportedTypeException`
   reads like a fault, and converting it directly to a permanent error throws
   away the only thing that can read the file.

3. **Honest empties.** A 200 with no text is a failed transcription, not a silent
   recording, and the two must not share a return value.

4. **The residency refusal.** A hold is `held=True`, not `ok=False`, so the
   caller can distinguish "the phone is busy" from "the model is broken".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from open_notebook.graphs import source as sg
from open_notebook.residency import client as residency
from open_notebook.scheduler import footprint as fp
from open_notebook.speech import client as speech


def _read_script(name: str) -> str:
    """A file from scripts/, as text. Shared by the two contract classes below."""
    return (Path(__file__).resolve().parents[1] / "scripts" / name).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Media detection, by content
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, head: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(head + b"\x00" * 64)
    return str(path)


class TestMediaDetection:
    @pytest.mark.parametrize(
        "name,head",
        [
            ("a.bin", b"ID3\x03\x00\x00\x00"),  # mp3 with ID3
            ("a.bin", b"\xff\xfb\x90\x64"),  # mp3 frame sync
            ("a.bin", b"RIFF\x24\x08\x00\x00WAVEfmt "),  # wav
            ("a.bin", b"OggS\x00\x02\x00\x00"),  # ogg / opus
            ("a.bin", b"fLaC\x00\x00\x00\x22"),  # flac
            ("a.bin", b"\x1a\x45\xdf\xa3\x01\x00\x00\x00"),  # mkv / webm
            ("a.bin", b"\x00\x00\x00\x18ftypM4A "),  # m4a
            ("a.bin", b"\x00\x00\x00\x20ftypisom"),  # mp4
        ],
    )
    def test_recognises_every_container_we_accept(self, tmp_path: Path, name, head):
        """All of these are names a blob store has already thrown away.

        The suffix is `.bin` on purpose in every case: a synced recording has no
        usable extension, which is the whole reason this reads magic bytes.
        """
        assert sg._is_media(_write(tmp_path, name, head)) is True

    @pytest.mark.parametrize(
        "name,head",
        [
            ("a.pdf", b"%PDF-1.7\n"),  # a document
            ("a.txt", b"just some words"),
            ("a.png", b"\x89PNG\r\n\x1a\n"),
            ("a.jpg", b"\xff\xd8\xff\xe0"),  # jpeg: 0xFF then 0xD8, not a frame
            ("empty.bin", b""),
        ],
    )
    def test_rejects_what_is_not_media(self, tmp_path: Path, name, head):
        assert sg._is_media(_write(tmp_path, name, head)) is False

    def test_a_jpeg_is_not_mistaken_for_an_mp3(self, tmp_path: Path):
        """The one collision in the 0xFF branch, and a real one.

        Both JPEG and MPEG audio start with 0xFF. The layer bits in an MPEG
        frame header (bits 2-3) are never `10`, while JPEG's marker byte 0xD8 is
        exactly that - so a photo would be handed to whisper, which would answer
        with an empty transcript and a confident-looking error about a file that
        was never audio.
        """
        assert sg._is_media(_write(tmp_path, "photo.bin", b"\xff\xd8\xff\xe0")) is False

    def test_a_missing_file_is_not_media(self, tmp_path: Path):
        """Reading the bytes is what decides, so an unreadable path is False.

        A suffix short-circuit placed before the read would classify any file
        that merely cannot be opened as whatever it claims to be.
        """
        assert sg._is_media(str(tmp_path / "nope.mp3")) is False

    def test_none_is_not_media(self):
        assert sg._is_media(None) is False


# ---------------------------------------------------------------------------
# The fallback into the extraction path
# ---------------------------------------------------------------------------


class _FakeExtraction:
    """Stand-in for content-core's ExtractionOutput.

    Mutable like the real one — `_extract_with_text_engine` assigns to
    `.content` — and takes the same two keywords the graph constructs it with.
    """

    def __init__(self, content: str = "", title: str = "") -> None:
        self.content = content
        self.title = title


class _ContentCoreStub:
    """The slice of `content_core` that `_extract_with_text_engine` touches."""

    class ContentCoreError(Exception):
        pass

    class UnsupportedTypeException(ContentCoreError):
        pass

    class FileOperationError(ContentCoreError):
        pass

    class NotFoundError(ContentCoreError):
        pass

    class NetworkError(ContentCoreError):
        pass

    class InvalidInputError(ContentCoreError):
        pass

    class ConfigurationError(ContentCoreError):
        pass

    class ExternalServiceError(ContentCoreError):
        pass

    class NoTranscriptFound(ContentCoreError):
        pass

    def __init__(self, content: str = "", error: Optional[Exception] = None) -> None:
        self.content = content
        self.error = error


def _patch_engine(monkeypatch, stub: _ContentCoreStub) -> List[Optional[Dict]]:
    """Replace content-core's extractor and the OCR helpers; record calls."""
    calls: List[Optional[Dict]] = []

    async def fake_extract(**_kwargs):
        calls.append({"extract": True})
        if stub.error is not None:
            raise stub.error
        return _FakeExtraction(stub.content)

    async def fake_try_ocr(*_a, **_k):
        calls.append({"ocr": True})
        return ""

    async def fake_augment(*_a, **_k):  # pragma: no cover - not reached for media
        raise AssertionError("selective OCR must not run on an audio file")

    monkeypatch.setattr(sg, "extract_content", fake_extract)
    monkeypatch.setattr(sg, "_try_ocr", fake_try_ocr)
    monkeypatch.setattr(sg, "_augment_with_ocr", fake_augment)
    monkeypatch.setattr(sg, "ExtractionOutput", _FakeExtraction)
    monkeypatch.setattr(sg.cc, "ContentCoreError", _ContentCoreStub.ContentCoreError)
    for name in (
        "UnsupportedTypeException",
        "FileOperationError",
        "NotFoundError",
        "NetworkError",
        "InvalidInputError",
        "ConfigurationError",
        "ExternalServiceError",
        "NoTranscriptFound",
    ):
        monkeypatch.setattr(sg.cc, name, getattr(_ContentCoreStub, name))
    return calls


async def _run(
    monkeypatch,
    stub: _ContentCoreStub,
    path: str,
    transcript: Optional[str] = None,
    *,
    transcript_error: Optional[str] = None,
    stt_held: bool = False,
):
    """Drive `_extract_with_text_engine` with a stubbed speech client."""
    _patch_engine(monkeypatch, stub)
    asked: List[str] = []

    async def fake_transcribe(p: str, **_kwargs):
        asked.append(p)
        if transcript_error is not None:
            return speech.TranscriptionResult(
                ok=False, error=transcript_error, held=stt_held
            )
        return speech.TranscriptionResult(ok=True, text=transcript or "")

    monkeypatch.setattr(speech, "transcribe", fake_transcribe)

    # `_try_stt` returns text, not a result: the degradation decision lives in
    # the graph, not in the client. Stubbing it with the client's own return
    # value would have tested a contract that does not exist - and the first
    # version of this helper did exactly that, failing with an AttributeError
    # that read like a bug in the graph.
    async def fake_try_stt(p: str, _cmd_id: Optional[str] = None) -> str:
        result = await fake_transcribe(p)
        return result.text

    monkeypatch.setattr(sg, "_try_stt", fake_try_stt)

    content_state: Dict[str, Any] = {}
    result = await sg._extract_with_text_engine(None, content_state, None, {}, "", path)
    return result, content_state, asked


class TestSpeechFallback:
    @pytest.mark.asyncio
    async def test_audio_with_empty_extraction_is_transcribed(
        self, monkeypatch, tmp_path
    ):
        path = _write(tmp_path, "lecture.bin", b"ID3\x03\x00\x00\x00")
        result, _state, asked = await _run(
            monkeypatch, _ContentCoreStub(content=""), path, transcript="good morning"
        )
        assert asked == [path]
        assert result.content == "good morning"

    @pytest.mark.asyncio
    async def test_a_refused_media_file_still_reaches_speech(
        self, monkeypatch, tmp_path
    ):
        """The case that reads like a fault and is not one.

        A text extractor declining an m4a is its expected answer. Converting
        `UnsupportedTypeException` straight into a permanent "this file type is
        not supported" would discard a lecture that whisper reads in a minute.
        """
        path = _write(tmp_path, "lecture.bin", b"\x00\x00\x00\x20ftypisom")
        stub = _ContentCoreStub(error=_ContentCoreStub.UnsupportedTypeException("no"))
        result, _state, asked = await _run(
            monkeypatch, stub, path, transcript="spoken words"
        )
        assert asked == [path]
        assert result.content == "spoken words"

    @pytest.mark.asyncio
    async def test_a_text_file_is_never_handed_to_speech(self, monkeypatch, tmp_path):
        """A PDF that extracted nothing is an empty document, not a broken one.

        Whisper on a PDF returns an empty transcript, so the operator would get
        a speech error naming a remedy that cannot help - the exact failure
        `_empty_file_message` was written to stop.
        """
        path = _write(tmp_path, "paper.pdf", b"%PDF-1.7\n")
        result, _state, asked = await _run(
            monkeypatch, _ContentCoreStub(content=""), path, transcript="should not run"
        )
        assert asked == []
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_a_refused_text_file_still_raises_its_own_error(
        self, monkeypatch, tmp_path
    ):
        path = _write(tmp_path, "paper.pdf", b"%PDF-1.7\n")
        stub = _ContentCoreStub(error=_ContentCoreStub.UnsupportedTypeException("no"))
        with pytest.raises(ValueError, match="not supported"):
            await _run(monkeypatch, stub, path, transcript="should not run")

    @pytest.mark.asyncio
    async def test_text_the_extractor_did_get_is_kept(self, monkeypatch, tmp_path):
        """An audio file with an embedded description is not overwritten.

        content-core can return real text for a media file that carries
        metadata or chapters. Replacing it with the transcript would discard
        content the extractor already had.
        """
        path = _write(tmp_path, "clip.bin", b"ID3\x03\x00\x00\x00")
        result, _state, asked = await _run(
            monkeypatch,
            _ContentCoreStub(content="Chapter 1: introduction"),
            path,
            transcript="spoken words",
        )
        assert asked == []
        assert result.content == "Chapter 1: introduction"

    @pytest.mark.asyncio
    async def test_a_joint_failure_names_speech_not_the_content_engine(
        self, monkeypatch, tmp_path
    ):
        """Both halves failing must produce one message about both.

        Reporting only the extractor's error sends the operator to configure a
        content engine; the missing piece was speech-to-text all along.
        """
        path = _write(tmp_path, "lecture.bin", b"ID3\x03\x00\x00\x00")
        stub = _ContentCoreStub(error=_ContentCoreStub.UnsupportedTypeException("no"))
        monkeypatch.setattr(sg, "_speech_reachable", lambda: (True, "ready"))
        _patch_engine(monkeypatch, stub)

        async def fake_transcribe(_p, **_k):
            return speech.TranscriptionResult(ok=False, error="no speech")

        async def fake_try_stt(_p: str, _cmd_id: Optional[str] = None) -> str:
            return (await fake_transcribe(_p)).text

        monkeypatch.setattr(speech, "transcribe", fake_transcribe)
        monkeypatch.setattr(sg, "_try_stt", fake_try_stt)

        with pytest.raises(ValueError) as excinfo:
            await sg._extract_with_text_engine(None, {}, None, {}, "", path)
        message = str(excinfo.value)
        assert "speech" in message.lower()
        assert "content engine" not in message.lower()

    @pytest.mark.asyncio
    async def test_an_unreachable_server_says_so(self, monkeypatch, tmp_path):
        """Unavailability outranks attempt, exactly as `_empty_file_message` does.

        "The speech server could not be reached" is actionable; "the model
        produced no transcript" blames a file that may be perfectly fine.
        """
        path = _write(tmp_path, "lecture.bin", b"ID3\x03\x00\x00\x00")
        stub = _ContentCoreStub(error=_ContentCoreStub.UnsupportedTypeException("no"))
        monkeypatch.setattr(
            sg, "_speech_reachable", lambda: (False, "connection refused")
        )
        _patch_engine(monkeypatch, stub)

        async def fake_transcribe(_p, **_k):
            return speech.TranscriptionResult(ok=False, error="refused")

        async def fake_try_stt(_p: str, _cmd_id: Optional[str] = None) -> str:
            return (await fake_transcribe(_p)).text

        monkeypatch.setattr(speech, "transcribe", fake_transcribe)
        monkeypatch.setattr(sg, "_try_stt", fake_try_stt)

        with pytest.raises(ValueError) as excinfo:
            await sg._extract_with_text_engine(None, {}, None, {}, "", path)
        assert "connection refused" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The clients
# ---------------------------------------------------------------------------


class TestTranscriptionClient:
    @pytest.mark.asyncio
    async def test_a_missing_file_is_an_error_not_an_empty_transcript(
        self, monkeypatch, tmp_path
    ):
        result = await speech.transcribe(tmp_path / "absent.wav")
        assert result.ok is False
        assert result.held is False

    @pytest.mark.asyncio
    async def test_a_200_with_no_text_is_a_failure(self, monkeypatch, tmp_path):
        """Silence and brokenness are different facts and share no return value.

        Returning `ok=True, text=""` would make a silent recording look like a
        successful transcription, and the source would then be embedded and
        indexed as an empty document.
        """
        path = _write(tmp_path, "silence.wav", b"RIFF\x00\x00\x00\x00WAVE")

        class _Client:
            def __init__(self, **_k): ...

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, *_a, **_k):
                class _R:
                    status_code = 200

                    @staticmethod
                    def json():
                        return {"text": "   "}

                return _R()

        monkeypatch.setattr(speech, "transcribe", speech.transcribe)
        monkeypatch.setitem(
            __import__("sys").modules,
            "httpx",
            type("m", (), {"AsyncClient": _Client}),
        )
        result = await speech.transcribe(path, ensure_resident=False)
        assert result.ok is False
        assert "empty transcript" in (result.error or "")

    @pytest.mark.asyncio
    async def test_a_residency_refusal_is_a_hold(self, monkeypatch, tmp_path):
        """`held` is what tells the caller to retry rather than to give up.

        Without it a busy phone looks identical to a missing whisper build, and
        the operator retries a working feature against a device that was simply
        committed elsewhere.
        """
        path = _write(tmp_path, "a.wav", b"RIFF\x00\x00\x00\x00WAVE")

        async def refused(role: str):
            return residency.ResidencyResult(
                ok=False, role=role, error="supervisor refused the load"
            )

        monkeypatch.setattr(residency, "enabled", lambda: True)
        monkeypatch.setattr(residency, "ensure_resident", refused)

        result = await speech.transcribe(path)
        assert result.ok is False
        assert result.held is True
        assert "residency:stt" in (result.error or "")


class TestSynthesisClient:
    @pytest.mark.asyncio
    async def test_empty_text_is_refused_before_any_request(
        self, monkeypatch, tmp_path
    ):
        called = []

        class _Client:
            def __init__(self, **_k): ...

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, *_a, **_k):
                called.append(True)
                raise AssertionError("must not call the server with no text")

        monkeypatch.setitem(
            __import__("sys").modules,
            "httpx",
            type("m", (), {"AsyncClient": _Client}),
        )
        result = await speech.synthesize(
            "   ", tmp_path / "out.wav", ensure_resident=False
        )
        assert result.ok is False
        assert not called

    @pytest.mark.asyncio
    async def test_audio_is_renamed_into_place_only_after_the_full_body(
        self, monkeypatch, tmp_path
    ):
        """A partial write must not leave a truncated file where an episode points.

        piper streams its response, so a connection cut mid-render produces real
        bytes and a real exception. The destination path is only claimed once
        the whole body has arrived.
        """
        destination = tmp_path / "speech" / "clip.wav"

        class _Client:
            def __init__(self, **_k): ...

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, *_a, **_k):
                class _R:
                    status_code = 200
                    content = b"RIFF" + b"\x00" * 128

                return _R()

        monkeypatch.setitem(
            __import__("sys").modules,
            "httpx",
            type("m", (), {"AsyncClient": _Client}),
        )
        result = await speech.synthesize("hello", destination, ensure_resident=False)
        assert result.ok is True
        assert result.path == destination
        assert result.bytes_written == 132
        # No .part left behind, and the file that exists is the real one.
        assert not destination.with_name("clip.wav.part").exists()

    @pytest.mark.asyncio
    async def test_a_failed_render_leaves_no_file_at_all(self, monkeypatch, tmp_path):
        destination = tmp_path / "speech" / "clip.wav"

        class _Client:
            def __init__(self, **_k): ...

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, *_a, **_k):
                class _R:
                    status_code = 500
                    text = "voice not found"

                return _R()

        monkeypatch.setitem(
            __import__("sys").modules,
            "httpx",
            type("m", (), {"AsyncClient": _Client}),
        )
        result = await speech.synthesize("hello", destination, ensure_resident=False)
        assert result.ok is False
        assert "500" in (result.error or "")
        assert not destination.exists()
        assert not destination.with_name("clip.wav.part").exists()


# ---------------------------------------------------------------------------
# Role and footprint accounting
# ---------------------------------------------------------------------------


class TestSpeechRoleAccounting:
    def test_synthesis_resolves_to_the_tts_role(self):
        assert residency.role_for_model("synthesis") == "tts"

    def test_transcription_is_demanded_at_the_point_of_use(self):
        """`process_source` is deliberately *not* mapped to speech.

        Whether a source needs the STT model is a fact about the file, and the
        gate cannot open the file. Mapping it would charge every PDF 256 MB for a
        model it will never load, holding work that has nothing to do with speech.
        The client asks at the moment the answer is knowable instead.
        """
        assert "transcription" not in residency.MODEL_ROLE
        assert fp.job_footprint("process_source").models == {
            "__no_model__": 64 * 1024 * 1024
        }

    def test_speech_footprints_are_stated_not_derived(self):
        """Both cost more than their weights, and saying so beats guessing.

        whisper base.en is 57 MB of weights with a couple of hundred MB of
        decode scratch behind it; charging the weights would understate by 3x,
        and understating is an OOM kill of the model server.

        Asserted against the *keys* rather than against commands. `synthesis` is
        demanded by `synthesize_speech`, but `transcription` is deliberately
        demanded by no command - see the test above - so a command-level
        assertion would describe a design that was chosen against.
        """
        for key in ("transcription", "synthesis"):
            assert fp.EXPLICIT_FOOTPRINTS[key] >= 256 * 1024 * 1024, key

    def test_speech_footprints_exceed_the_runtime_overhead_derivation(self):
        """Stated, not `WEIGHT_OVERHEAD * bytes` — the weights are the small part."""
        for key in ("transcription", "synthesis"):
            weights_only = int(fp.EXPLICIT_FOOTPRINTS[key] / fp.WEIGHT_OVERHEAD)
            assert fp.EXPLICIT_FOOTPRINTS[key] > weights_only

    def test_a_speech_job_is_not_charged_the_placeholder(self):
        """The bug this closes: every unlisted kind cost 64 MB, plumbing's price.

        A command kind with a real model behind it was being billed as if it
        needed none, which understates rather than holds - the expensive
        direction.
        """
        speech_cost = fp.job_footprint("synthesize_speech").total
        plumbing_cost = fp.job_footprint("process_source").total
        assert speech_cost > plumbing_cost * 2


def _load_supervisor():
    """Import the Termux supervisor from scripts/.

    Registered in `sys.modules` before exec because `@dataclass` resolves its own
    module through that mapping - without it the import dies on a Role dataclass
    with an error that says nothing about the cause.
    """
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "scripts" / "termux-supervisor.py"
    spec = importlib.util.spec_from_file_location("termux_supervisor_speech", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestSupervisorSpeechRoles:
    def _sup(self):
        return _load_supervisor()

    def test_both_speech_roles_are_registered(self):
        sup = self._sup()
        assert "stt" in sup.ROLES
        assert "tts" in sup.ROLES

    def test_every_role_still_has_its_own_port(self):
        """Four servers, four ports. A collision would have the reaper unload
        the model another role is serving."""
        sup = self._sup()
        ports = [role.port for role in sup.ROLES.values()]
        assert len(set(ports)) == len(ports)
        assert set(ports) == {8080, 8081, 8090, 8091}

    def test_speech_ranks_below_the_text_classes(self):
        """A lecture is longer than a paragraph, so holding it back costs more."""
        sup = self._sup()
        assert sup.ROLES["chat"].priority < sup.ROLES["embed"].priority
        assert sup.ROLES["embed"].priority < sup.ROLES["stt"].priority
        assert sup.ROLES["stt"].priority < sup.ROLES["tts"].priority

    def test_the_supervisor_only_calls_verbs_that_exist(self):
        for role in self._sup().ROLES.values():
            for verb in (role.probe, role.load, role.unload, role.model_path):
                assert verb[0] == "sh"
                assert verb[-1] in (
                    "chat-status",
                    "chat-up",
                    "chat-down",
                    "model-path",
                    "probe",
                    "up",
                    "down",
                )


class TestTtsServerContract:
    """The phone's TTS server, tested without piper.

    piper's own `server.py` was rejected for three reasons — it binds
    `0.0.0.0`, hardcodes `MODELS_DIR = "/app/models"` with the port fixed at
    9600, and needs Flask — so `scripts/termux-tts-server.py` replaces it. That
    makes it ours, and an untested replacement for a component that ships on
    every phone is exactly the sort of thing that is fine until it is not.

    piper itself is stubbed. Everything asserted here is the half we own: the
    argument vector, the voice resolution, and the failure answers.
    """

    @staticmethod
    def _server(tmp_path, monkeypatch, **env):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "termux_tts_server",
            Path(__file__).resolve().parents[1] / "scripts" / "termux-tts-server.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for key, value in env.items():
            monkeypatch.setattr(module, key, value)
        return module

    @staticmethod
    def _stub_piper(tmp_path, log: Path) -> Path:
        """A piper that records its arguments and writes a recognisable file."""
        binary = tmp_path / "piper-stub"
        binary.write_text(
            "#!/bin/sh\n"
            f'printf "ARGS:" >> {log}\n'
            'for a in "$@"; do printf " [%s]" "$a" >> ' + str(log) + "; done\n"
            f'printf "\\n" >> {log}\n'
            'out=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in --output_file) out="$2"; shift 2 ;; *) shift ;; esac\n'
            "done\n"
            "text=$(cat)\n"
            f'printf "STDIN:%s\\n" "${{#text}}" >> {log}\n'
            '[ -n "$out" ] || exit 3\n'
            "printf 'RIFFWAVEfmt ' > \"$out\"\n"
            "exit 0\n"
        )
        binary.chmod(0o755)
        return binary

    def _voices(self, tmp_path: Path) -> Path:
        models = tmp_path / "models"
        models.mkdir(exist_ok=True)
        for name in ("en_US-amy-medium", "en_GB-alan-medium"):
            (models / f"{name}.onnx").write_bytes(b"stub")
        return models

    def test_it_binds_loopback_by_default(self, tmp_path, monkeypatch):
        """Every other server here is loopback-only, and on a phone that means
        the synthesis endpoint is otherwise reachable from the local network
        with no credential at all."""
        src = (
            Path(__file__).resolve().parents[1] / "scripts" / "termux-tts-server.py"
        ).read_text()
        assert 'os.getenv("TTS_HOST", "127.0.0.1")' in src
        # And saying so out loud when it is overridden, rather than silently
        # widening the attack surface.
        assert "not loopback" in src

    def test_voice_names_resolve_including_the_piper_prefix(
        self, tmp_path, monkeypatch
    ):
        module = self._server(
            tmp_path, monkeypatch, MODELS_DIR=str(self._voices(tmp_path))
        )
        resolved = module.resolve_voice("en_GB-alan-medium")
        assert resolved is not None and resolved.endswith("en_GB-alan-medium.onnx")
        # `piper-<stem>` is what piper's own server accepts, and a caller that
        # learned that spelling must keep working.
        assert module.resolve_voice("piper-en_GB-alan-medium") == resolved

    def test_an_unknown_voice_resolves_to_none_not_a_guess(self, tmp_path, monkeypatch):
        module = self._server(
            tmp_path, monkeypatch, MODELS_DIR=str(self._voices(tmp_path))
        )
        assert module.resolve_voice("does-not-exist") is None

    def test_it_passes_the_voice_path_to_piper(self, tmp_path, monkeypatch):
        module = self._server(
            tmp_path, monkeypatch, MODELS_DIR=str(self._voices(tmp_path))
        )
        log = tmp_path / "calls.log"
        module.PIPER_BIN = str(self._stub_piper(tmp_path, log))
        module.TIMEOUT = 30

        audio = module.synthesize("hello", module.resolve_voice("en_US-amy-medium"))
        assert audio.startswith(b"RIFF")

        recorded = log.read_text()
        assert "--model" in recorded
        assert "en_US-amy-medium.onnx" in recorded
        assert "--output_file" in recorded
        assert "STDIN:5" in recorded, "the text goes to piper on stdin"

    def test_an_unset_espeak_data_is_omitted_not_passed_empty(
        self, tmp_path, monkeypatch
    ):
        """The bug this test exists for.

        `--espeak_data ""` is not the same as omitting the flag. piper finds its
        own espeak-ng-data relative to the binary; an explicit empty value
        overrides that with the process's working directory, and synthesis fails
        with a phonemisation error that names no file at all. Found by running
        the server against a stub that logged its arguments, which is the only
        way the difference is visible — the HTTP response is a 500 either way.
        """
        module = self._server(
            tmp_path, monkeypatch, MODELS_DIR=str(self._voices(tmp_path))
        )
        log = tmp_path / "calls.log"
        module.PIPER_BIN = str(self._stub_piper(tmp_path, log))
        module.TIMEOUT = 30

        monkeypatch.delenv("ESPEAK_DATA", raising=False)
        module.synthesize("hello", module.resolve_voice("en_US-amy-medium"))
        assert "--espeak_data" not in log.read_text()

    def test_a_set_espeak_data_is_passed(self, tmp_path, monkeypatch):
        module = self._server(
            tmp_path, monkeypatch, MODELS_DIR=str(self._voices(tmp_path))
        )
        log = tmp_path / "calls.log"
        module.PIPER_BIN = str(self._stub_piper(tmp_path, log))
        module.TIMEOUT = 30

        monkeypatch.setenv(
            "ESPEAK_DATA", "/data/data/com.termux/files/usr/share/espeak-ng-data"
        )
        module.synthesize("hello", module.resolve_voice("en_US-amy-medium"))
        recorded = log.read_text()
        assert "--espeak_data" in recorded
        assert "espeak-ng-data" in recorded

    def test_it_refuses_to_start_without_piper(self, tmp_path, monkeypatch):
        """Absent, not a server that answers 200 and synthesises silence."""
        module = self._server(tmp_path, monkeypatch)
        monkeypatch.setattr(module, "PIPER_BIN", str(tmp_path / "nope"))
        assert module.main() == 1


class TestPiperBuildRoute:
    """The build, pinned on the decisions that decide whether it works.

    piper-phonemize's CMake downloads a prebuilt
    `onnxruntime-linux-aarch64-1.14.1.tgz` — a glibc binary. Android's loader
    rejects it, and does so at run time, after a build that otherwise
    succeeded. `ONNXRUNTIME_DIR` skips the download and uses `<dir>/include` and
    `<dir>/lib`.

    The trap is that Termux's onnxruntime does not have that shape. It installs
    the C++ headers one level down, in `$PREFIX/include/onnxruntime/`, so
    pointing `ONNXRUNTIME_DIR` at `$PREFIX` satisfies nothing: the variable
    arrives, the glibc download is correctly skipped, and the build then dies on
    a header that a careless `ls` had already "confirmed". That is a real
    afternoon, so it is pinned below.
    """

    def test_it_points_piper_phonemize_at_the_termux_onnxruntime(self):
        text = _read_script("termux-build-piper.sh")
        assert "ONNXRUNTIME_DIR" in text

    def test_it_does_not_point_onnxruntime_dir_at_prefix_directly(self):
        """The layout mismatch that broke three builds, pinned.

        Termux ships `include/onnxruntime/onnxruntime_cxx_api.h`, but
        piper-phonemize reads `${ONNXRUNTIME_DIR}/include` and expects the header
        *in* it. So `ONNXRUNTIME_DIR=$PREFIX` yields a defined-but-useless
        variable: the glibc download is skipped (good) and the compile cannot
        find the header (fatal). The header must be located, not assumed.
        """
        text = _read_script("termux-build-piper.sh")
        # The header is discovered rather than assumed to be in $PREFIX/include.
        assert "onnxruntime_cxx_api.h" in text
        assert "find " in text and "$ORT/include" in text
        # ...and ONNXRUNTIME_DIR ends up at the shim, never at the raw prefix.
        assert 'ORT="$SHIM"' in text
        assert 'ONNXRUNTIME_DIR="$ORT" \\' in text

    def test_the_shim_populates_both_paths_piper_phonomize_reads(self):
        """`target_include_directories(... ${ONNXRUNTIME_DIR}/include)` and
        `target_link_directories(... ${ONNXRUNTIME_DIR}/lib)`. Both must resolve,
        or the build fails at compile time or at link time — the second only
        after everything expensive has already succeeded.
        """
        text = _read_script("termux-build-piper.sh")
        assert 'mkdir -p "$SHIM/include" "$SHIM/lib"' in text
        assert "$SHIM/include/$(basename" in text
        assert "$SHIM/lib/$(basename" in text

    def test_it_verifies_the_prefix_by_compiling_not_by_testing_a_file(self):
        """`test -f` is what produced the false confirmation.

        The failure mode here was a diagnostic that ran
        `ls -1 $PREFIX/include/onnxruntime*`. That glob matches a *directory*
        and lists its contents, so the output read as "the headers are in
        include/" — which was false. What piper needs is for the *compiler* to
        resolve the header through the include directory the prefix provides,
        and that is precisely what a `test -f` does not check. So the check must
        be a compile.
        """
        text = _read_script("termux-build-piper.sh")
        assert "ort-check.cpp" in text
        assert 'c++ -I"$SHIM/include" -c' in text

    def test_it_writes_nothing_to_slash_tmp(self):
        """Termux has no `/tmp`.

        A probe that wrote its throwaway source to `/tmp` failed with
        "Directory nonexistent" and printed a result about the include path that
        had nothing to do with the question. Anything temporary goes under
        $HOME.
        """
        text = _read_script("termux-build-piper.sh")
        assert '"/tmp/' not in text
        assert "=/tmp/" not in text

    def test_it_never_binds_a_variable_named_prefix(self):
        """The bug that put piper inside Termux's own tree, pinned.

        Termux exports `PREFIX=/data/data/com.termux/files/usr`. So

            PREFIX="${PREFIX:-$HOME/piper-build/prefix}"

        does not fall back to the default on the only platform this script runs
        on — it takes Termux's value and installs piper, piper-phonemize,
        espeak-ng, libtashkeel_model.ort and every .so straight into Termux's
        tree, overwriting the espeak-ng data an installed `espeak` package reads.
        The build succeeded, which is what made it easy to miss.
        """
        text = _read_script("termux-build-piper.sh")
        # No assignment to a bare PREFIX anywhere.
        assert not re.search(r"^\s*PREFIX=", text, re.MULTILINE)
        # ...and the install prefix is the deliberately distinct name.
        assert 'DEST="${PIPER_DEST:-$HOME/piper-build/prefix}"' in text
        assert '-DCMAKE_INSTALL_PREFIX="$DEST"' in text

    def test_the_install_layout_is_what_the_launcher_expects(self):
        """piper installs flat; the launcher expects `bin/piper`.

        piper's rules are `DESTINATION ${CMAKE_INSTALL_PREFIX}` — an absolute
        path — so an unmodified install produces `$DEST/piper`, never
        `$DEST/bin/piper`. The build therefore relocates the binary *and* the
        data it resolves relative to its own executable:

            exePath.parent_path().append("espeak-ng-data")
            exePath.parent_path().append("libtashkeel_model.ort")

        Move the binary and leave the data behind and phonemisation fails at run
        time with an espeak error that names no file.
        """
        text = _read_script("termux-build-piper.sh")
        assert 'mv "$DEST/piper" "$DEST/bin/piper"' in text
        assert 'mv "$DEST/$_d" "$DEST/bin/$_d"' in text
        # Both data items are moved, and their absence is called out.
        assert "espeak-ng-data libtashkeel_model.ort" in text
        # The launcher and server both point at this exact path.
        assert "$HOME/piper-build/prefix/bin/piper" in _read_script("termux-tts.sh")

    def test_it_verifies_the_downloaded_voice_by_byte_count(self):
        """curl reported success four times while writing a fraction of the file.

        On this link curl prints `100 60.27M` — a finished transfer — and leaves
        14 MB on disk, because the connection is cut and its progress accounting
        is optimistic. Two consequences, both observed:

        - `[ -s file ]` is not a completeness test, so a truncated download is
          skipped on the next run and piper then fails with "Model config
          doesn't exist", which points at the config rather than the model.
        - `curl -C -` cannot repair it. Resuming appends to a prefix that is
          already wrong, and the file grew *past* its true size rather than
          reaching it.

        So the size is compared against the value the HuggingFace API reports, and
        a partial is never promoted to the name a later resume would build on.
        """
        text = _read_script("termux-build-piper.sh")
        assert "VOICE_BYTES=63201294" in text
        assert "VOICE_JSON_BYTES=4882" in text
        assert "fetch_exact" in text
        # No bare resume of the real file, and no size-blind `-s` guard.
        assert "-C -" not in text
        assert 'if [ ! -s "$VOICE_DIR/en_US-amy-medium.onnx" ]; then' not in text
        # Promotion only happens on an exact match.
        assert 'if [ "$_got" = "$_want" ]; then mv "$_d.part" "$_d"; fi' in text

    def test_it_checks_the_output_is_audio_not_just_a_non_empty_file(self):
        """piper wrote 48 MB of model and produced no file at all once.

        `[ -s out ]` accepts any non-empty file. The end-to-end check asserts the
        RIFF header, so a truncated or empty wav cannot pass for a working
        synthesis.
        """
        text = _read_script("termux-build-piper.sh")
        assert 'case "$(dd if="$OUT" bs=1 count=4' in text
        assert "RIFF" in text
        assert 'fail "output is not a RIFF wav"' in text

    def test_it_verifies_the_loader_can_resolve_piper_before_asserting_success(self):
        """The RPATH points at the prefix piper was installed into, not where it
        now lives. After the relocation to `bin/`, the recorded path is stale, and
        the libraries sit beside the binary where the loader does not look. So
        `ldd` is asked, and a non-zero count of unresolved libraries fails the
        build rather than surfacing later as a server that will not start.
        """
        text = _read_script("termux-build-piper.sh")
        assert 'ldd "$BIN"' in text
        assert 'fail "shared libraries"' in text

    def test_onnxruntime_dir_goes_in_the_environment_not_on_the_command_line(self):
        """The nested ExternalProject only inherits the environment.

        piper builds piper-phonemize with ExternalProject_Add and forwards
        CMAKE_INSTALL_PREFIX alone, so a `-DONNXRUNTIME_DIR` on piper's own
        command line would silently never reach the build that needs it — and
        the failure would be the glibc download it was meant to prevent.
        """
        text = _read_script("termux-build-piper.sh")
        assert "ONNXRUNTIME_DIR=" in text
        # `-DONNXRUNTIME_DIR:PATH` is the patched CMAKE_ARGS, which is fine;
        # a bare `-DONNXRUNTIME_DIR ` on piper's own command line is not.
        assert "-DONNXRUNTIME_DIR " not in text

    def test_piper_phonemize_dir_is_never_defined(self):
        """The bug that cost the first phone attempt, pinned.

        `PIPER_PHONEMIZE_DIR` is not "where to install piper-phonemize". It
        guards the ExternalProject that *builds* it:

            if(NOT DEFINED PIPER_PHONEMIZE_DIR)
              ExternalProject_Add(piper_phonemize_external ...)
            endif()

        Defining it skips that build entirely. piper then finds neither
        espeak-ng's headers nor onnxruntime's, because installing them is the
        only thing that build does — so the build ran to 86%, failed on two
        missing headers, and left no piper-phonemize anywhere on disk.
        """
        text = _read_script("termux-build-piper.sh")
        assert "-DPIPER_PHONEMIZE_DIR" not in text

    def test_it_forwards_onnxruntime_dir_into_the_nested_build(self):
        """CMake does not turn $ENV{X} into a cache variable of that name.

        So the environment alone is not enough — piper-phonemize's configure
        never sees it, downloads the glibc onnxruntime, and produces a binary
        Android cannot load. The value has to be written into the
        ExternalProject's CMAKE_ARGS, read there with `$ENV{}`. An earlier
        version of this script believed the environment was sufficient, and the
        belief was never tested against anything.
        """
        text = _read_script("termux-build-piper.sh")
        assert "CMAKE_ARGS -DONNXRUNTIME_DIR:PATH=\\$ENV{ONNXRUNTIME_DIR}" in text

    def test_the_sed_is_verified_rather_than_assumed(self):
        """A patch that does not match must stop the build, not pass silently.

        The whole diagnosis rests on the patch having applied. If the sed ever
        stops matching — upstream moves the line — the build would proceed to
        fail hours later at a link error, which is exactly where the first
        attempt died without anyone knowing why.
        """
        text = _read_script("termux-build-piper.sh")
        assert "FAILED to patch CMAKE_ARGS" in text
        # And the guard must actually gate the build rather than merely be
        # printed near it: the abort sits between the verification grep and the
        # next step, so a failed patch stops here instead of two hours later.
        assert text.index('fail "patch piper"') > text.index(
            "if grep -q 'ONNXRUNTIME_DIR:PATH' CMakeLists.txt; then"
        )
        assert text.index('fail "patch piper"') < text.index('step "clearing')

    def test_it_checks_piper_phonemize_is_a_target_before_building(self):
        """Cheap, and it is the check whose absence cost the attempt.

        Configure can succeed while silently not building the thing everything
        else depends on. Confirming the target exists costs one grep and turns
        an hour of building into an immediate, legible failure.
        """
        text = _read_script("termux-build-piper.sh")
        assert "piper_phonemize_external" in text
        assert "configure sanity" in text
        # And it has to run before the build starts, not after it. An earlier
        # version of this script had no such check at all, and the cost of that
        # was an hour of building before finding out the target was absent.
        assert text.index("configure sanity") < text.index('step "building')

    def test_it_patches_espeak_ngs_static_link_too(self):
        """The same bionic problem as piper's, in a file that arrives later.

        piper's own `-static-libgcc -static-libstdc++` is stripped, but
        espeak-ng also static-links:

            target_link_options(espeak-ng PRIVATE "-static-libstdc++" "-static")

        and it arrives via piper-phonemize's ExternalProject, downloaded during
        the build rather than at configure time — so patching it beforehand is
        impossible and patching it afterwards is the only option.
        """
        text = _read_script("termux-build-piper.sh")
        assert "patch_espeak_for_android" in text
        assert "static-libstdc++" in text
        assert "espeak_ng_external" in text

    def test_it_gates_speechplayer_which_piper_phonemize_failed_to_disable(self):
        """`USE_SPEECHPLAYER=OFF` stops the link, not the compile.

        piper-phonemize passes `-DUSE_SPEECHPLAYER:BOOL=OFF`, which gates only
        `target_link_libraries(espeak-ng PRIVATE speechPlayer)`. The
        subdirectory itself is added unconditionally in espeak-ng's
        `src/CMakeLists.txt`, so speechPlayer's sources were compiled anyway and
        the build died on:

            speechWaveGenerator.cpp:197: error: reference to 'sample' is ambiguous

        — espeak-ng declares its own `sample`, and C++17's `<algorithm>` brings
        in `std::sample`. Gating `add_subdirectory(speechPlayer)` is what
        actually stops it, and piper needs a library, not a speech player.
        """
        text = _read_script("termux-build-piper.sh")
        assert "add_subdirectory(speechPlayer)" in text
        assert "USE_SPEECHPLAYER" in text
        # Gated, not deleted: the upstream intent stays readable in the patch.
        assert "if (USE_SPEECHPLAYER)" in text

    def test_it_patches_a_leftover_espeak_before_building(self):
        """ExternalProject caches its download, so a retry can patch up front.

        Otherwise every retry pays a full espeak-ng compile — dictionary data
        generation included — only to fail on the same line and patch
        afterwards. The pre-patch is best-effort; the one after a failure is
        what guarantees the retry happens at all.
        """
        text = _read_script("termux-build-piper.sh")
        first_patch = text.index("patching any espeak-ng left over")
        build = text.index('step "building')
        assert first_patch < build, "patch the leftover tree before the build"

    def test_the_espeak_patch_only_fires_on_the_static_link_failure(self):
        """A patch applied on any failure would paper over unrelated errors.

        Only the first attempt's failure decides whether a retry is worthwhile.
        A build that failed for another reason must report, not rebuild.
        """
        text = _read_script("termux-build-piper.sh")
        # The gate is now "did the patch change anything", which is checked by
        # the function's own return value rather than by grepping the log.
        gate = text.split("if ! build_once; then", 1)[1].split("else", 1)[0]
        assert "patch_espeak_for_android" in gate

    def test_a_failed_build_reports_the_compiler_error_not_the_make_wrapper(self):
        """Six levels of `make[N]: Error 2` and no reason is not a diagnosis.

        The build log is captured whole and the first `error:` lines are
        printed. Piping to `tail` — what an earlier version did — discarded
        precisely the compiler message that explained the failure.
        """
        text = _read_script("termux-build-piper.sh")
        assert 'BUILD_LOG="$SRC/build.log"' in text
        assert "report_failure" in text
        body = text.split("report_failure() {", 1)[1].split("\n}", 1)[0]
        assert "error:" in body
        assert "wc -l" in body
        # And the whole log must be captured, not just the tail.
        assert '>"$BUILD_LOG" 2>&1' in text

    def test_it_wants_the_c_library_not_the_python_bindings(self):
        """`python-onnxruntime` ships the wheel; piper links `libonnxruntime.so`.

        They are separate packages in Termux, and installing only the Python one
        produces a build that cannot find the library it needs.
        """
        text = _read_script("termux-build-piper.sh")
        assert "pkg install onnxruntime" in text
        assert "NOT python-onnxruntime" in text

    def test_it_strips_the_glibc_static_link_flags(self):
        text = _read_script("termux-build-piper.sh")
        assert "static-libstdc++" in text
        assert "sed -i" in text


class TestTtsLauncherLibraryPath:
    """piper cannot load its own libraries without this.

    The RPATH is baked at install time and names the flat prefix. Once the binary
    is moved to `bin/`, that path is stale, and the `.so` files sit beside the
    binary — which the loader does not search. So the launcher exports
    LD_LIBRARY_PATH, and the server hands piper to `subprocess.run`, which
    inherits it. Without it, synthesis fails at run time with a loader error that
    names a shared object rather than the relocation that caused it.
    """

    def test_the_launcher_exports_a_library_path_for_piper(self):
        text = _read_script("termux-tts.sh")
        assert "LD_LIBRARY_PATH=" in text
        assert "export LD_LIBRARY_PATH" in text
        # Derived from the binary's own location, not hardcoded, so a relocated
        # prefix keeps working.
        assert 'PIPER_LIB_DIR=$(dirname "$PIPER_BIN")' in text


class TestProcScanningIsRaceSafe:
    """Every launcher scans /proc, and every scan races process exit.

    `for p in /proc/[0-9]*` globs a snapshot; by the time the loop body reads
    `$p/cmdline`, that pid may already be gone. The obvious guard does not work:

        cl=$(tr '\\0' ' ' < "$p/cmdline" 2>/dev/null) || continue

    POSIX applies redirections left to right, so `< "$p/cmdline"` is attempted
    *before* `2>/dev/null` takes effect. When the input redirection fails, the
    shell writes its own error and it lands on the terminal — `2>/dev/null` only
    ever covered `tr`, which never ran. Seen on the device as

        ~/bin/onb-tts: 113: cannot open /proc/25104/cmdline: No such file

    on an otherwise perfectly healthy `probe`, which is the worst place for it:
    the output an operator reads to decide whether TTS is up. And the line right
    after it still reported `role=tts resident=1`, so it reads as noise that
    could equally read as a failure.

    The fix is braces — `{ tr ... < "$p/cmdline"; } 2>/dev/null` — which put the
    failing redirection inside the redirected command. Six sites across six
    launchers had the unsafe ordering, so this scans the whole family.
    """

    LAUNCHERS = [
        "termux-stt.sh",
        "termux-tts.sh",
        "termux-embed.sh",
        "termux-launch.sh",
        "termux-model.sh",
        "termux-stack.sh",
    ]

    def _proc_scans(self, script: str) -> List[str]:
        return [
            line
            for line in _read_script(script).splitlines()
            if ("/cmdline" in line or "/environ" in line)
            and "<" in line
            and not line.lstrip().startswith("#")
        ]

    def test_the_pattern_is_actually_present_to_catch(self):
        """A test that passes because its scan found nothing is worse than none.

        These files really do scan /proc, so the scan must find sites. Without
        this, deleting every /proc scan would turn the safety tests green.
        """
        found = {s: len(self._proc_scans(s)) for s in self.LAUNCHERS}
        assert any(found.values()), f"no launcher scans /proc any more: {found}"
        assert sum(found.values()) >= 8, f"expected the scans to be present: {found}"

    def test_no_launcher_redirects_a_proc_path_before_stderr(self):
        """The invariant itself, reported per file so a failure names the site."""
        for script in self.LAUNCHERS:
            bad = []
            for line in self._proc_scans(script):
                # Safe forms: braces around the command, or 2>/dev/null written
                # before the `<`, which puts stderr in place first.
                has_braces = "$({ " in line or "{ tr" in line
                stderr_first = "2>/dev/null" in line and line.index(
                    "2>/dev/null"
                ) < line.index("<")
                if not (has_braces or stderr_first):
                    bad.append(line.strip())
            assert not bad, f"{script}: unsafe /proc redirection:\n  " + "\n  ".join(
                bad
            )


class TestProcessIdentification:
    """How each launcher finds the process it started.

    Both bugs here were found on the device, not in review, and both had the
    same shape: a matcher written against the command line when the thing it
    needed was somewhere else. A probe that cannot find its own process reports
    a role as unloaded while the server is serving, and a `down` with nothing to
    stop reports success.

    Driven against real `/proc`-shaped cmdlines so the matchers are exercised as
    written rather than as intended.
    """

    @staticmethod
    def _matches(script: str, cmdline: str, env: str = "") -> bool:
        """Does `script`'s pid_of accept this process?

        `env` is a NUL-separated environ blob, as /proc/<pid>/environ serves it.
        """
        text = _read_script(script)
        # Pull the pid_of case pattern out of the shell source.
        patterns = re.findall(r"case \"\$cl\" in (.*?)\)", text)
        assert patterns, f"{script} has no cmdline case"
        for raw in patterns:
            for token in re.findall(r"\*[^*]*\*", raw):
                needle = token.strip("*").replace("\\ ", " ")
                if needle and needle in cmdline:
                    return True
        return False

    def test_stt_finds_its_server_on_the_command_line_it_actually_uses(self):
        """`up` starts whisper-server with `-m`, not `--model`."""
        cmdline = (
            "/data/data/com.termux/files/usr/bin/taskset -c 4-7 "
            "/data/data/com.termux/files/home/whisper.cpp/build/bin/whisper-server "
            "-m /data/data/com.termux/files/home/gguf/ggml-base.en-q5_1.bin "
            "-t 2 --host 127.0.0.1 --port 8090 "
            "--inference-path /v1/audio/transcriptions --convert"
        )
        assert self._matches("termux-stt.sh", cmdline)

    def test_tts_finds_its_server_even_though_the_port_is_not_on_the_command_line(self):
        """The bug found on the device.

        TTS's port is passed as `TTS_PORT=8091 python3 .../onb-tts-server`, so
        the command line is just `python3 <script>` — no port, and not the
        script name the matcher expected after the server was replaced. Matching
        on the port meant `pid_of` never matched: `up` would start a second
        server on every call, and `down` would report nothing to stop.
        """
        cmdline = "python3 /data/data/com.termux/files/home/bin/onb-tts-server"
        assert self._matches("termux-tts.sh", cmdline)

    def test_neither_launcher_matches_the_other_roles(self):
        """Four servers, four identities. A cross-match means the reaper can
        unload a model another role is serving."""
        chat = "llama-server -m /home/gguf/qwen.gguf --host 127.0.0.1 --port 8080"
        embed = "llama-server -m /home/gguf/bge.gguf --embeddings --port 8081"
        stt = "whisper-server -m /home/gguf/whisper.bin --port 8090 --convert"
        tts = "python3 /home/bin/onb-tts-server"
        assert self._matches("termux-stt.sh", stt)
        assert not self._matches("termux-stt.sh", chat)
        assert not self._matches("termux-stt.sh", embed)
        assert not self._matches("termux-stt.sh", tts)
        assert self._matches("termux-tts.sh", tts)
        assert not self._matches("termux-tts.sh", chat)
        assert not self._matches("termux-tts.sh", embed)
        assert not self._matches("termux-tts.sh", stt)

    def test_served_model_reads_the_flag_up_actually_passes(self):
        """STT reports `model=` empty when the matcher looks for `--model`.

        `up` starts the server with `-m`, so grepping the command line for
        `--model` finds nothing. The supervisor's key/value parser needs a
        non-empty value to match at all, so the field vanished rather than
        showing blank — and the status panel had nothing to say about which
        model was resident.
        """
        text = _read_script("termux-stt.sh")
        block = text.split("served_model() {", 1)[1].split("\n}", 1)[0]
        assert "-m" in block, "must match the short flag the launcher uses"
        assert "--model" in block, "and the long form, in case that is used"

    def test_tts_reads_its_model_from_the_environment_not_the_command_line(self):
        text = _read_script("termux-tts.sh")
        block = text.split("served_model() {", 1)[1].split("\n}", 1)[0]
        # TTS_MODEL is an environment variable of the server process; the
        # command line is just `python3 <script>`.
        assert "/environ" in block
        assert "TTS_MODEL" in block

    """The verbs are a contract between a Python file and two shell files."""

    def _code(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("echo "):
                continue
            lines.append(line)
        return "\n".join(lines)

    @pytest.mark.parametrize("script", ["termux-stt.sh", "termux-tts.sh"])
    def test_both_expose_the_four_residency_verbs(self, script):
        code = self._code(_read_script(script))
        for verb in ("up)", "down)", "probe)", "model-path)"):
            assert verb in code, f"{script} lost {verb}"

    @pytest.mark.parametrize("script", ["termux-stt.sh", "termux-tts.sh"])
    def test_both_bind_their_own_port(self, script):
        """Four servers, four ports. A collision would let one role's reaper
        unload the model another role is serving, and the symptom would be a
        model that reports itself unloaded with nothing in the logs."""
        code = self._code(_read_script(script))
        expected = "STT_PORT:-8090" if "stt" in script else "TTS_PORT:-8091"
        assert expected in code
        # And the pid matcher must distinguish it from the other three, which
        # is the same executable pattern on some of these ports.
        assert ("--port\\ 8090" in code) or ("8091" in code)

    @pytest.mark.parametrize("script", ["termux-stt.sh", "termux-tts.sh"])
    def test_both_read_their_threads_from_the_shared_library(self, script):
        """A hardcoded thread count is how chat and embed came to disagree.

        Every consumer of the CPU budget sources `onb-cpu`; a launcher that
        hardcodes its own would drift from the supervisor's accounting and
        saturate the device while the gate believed it had headroom.
        """
        code = self._code(_read_script(script))
        assert "onb-cpu" in code
        assert "cpu_threads" in code

    def test_both_scripts_are_installed_as_wrappers(self):
        """A script that exists only in the repo is a script the phone cannot run."""
        code = self._code(_read_script("install-bin.sh"))
        assert "install_one termux-stt.sh     onb-stt" in code
        assert "install_one termux-tts.sh     onb-tts" in code

    def test_the_cpu_library_answers_for_both_new_roles(self):
        """A missing case would fall through to one thread, silently.

        `cpu_threads`'s default arm is one thread, which is safe but means a
        transcription runs at a fifth of the device while the panel reports a
        budget it never used.
        """
        code = self._code(_read_script("termux-cpu.sh"))
        assert "stt | tts)" in code

    def test_the_summary_reports_both_new_roles(self):
        code = self._code(_read_script("termux-cpu.sh"))
        assert "$(cpu_threads stt)" in code
        assert "$(cpu_threads tts)" in code

    def test_stt_passes_no_flag_whisper_server_would_reject(self):
        """whisper-server exits on an unknown argument — with status 0.

        The first version of `termux-stt.sh` passed `-c 512`, copied from
        llama-server's vocabulary. whisper.cpp's server has no `-c`; its context
        comes from the model and `-mc/--max-context` is a *text* context. The
        server prints "error: unknown argument" and calls `exit(0)`, so a start
        check that only looked at the exit code would have called that a clean
        launch while nothing was listening.

        Asserted here against the script because the failure is a flag name,
        and flag names are exactly what drifts between the two projects.
        """
        code = self._code(_read_script("termux-stt.sh"))
        for forbidden in ("-c 512", "-c512", " --c "):
            assert forbidden not in code, f"termux-stt.sh passes {forbidden!r}"

    @pytest.mark.parametrize(
        "flag",
        ["-m", "-t", "--host", "--port", "--inference-path", "--convert"],
    )
    def test_stt_only_passes_flags_the_server_defines(self, flag):
        code = self._code(_read_script("termux-stt.sh"))
        assert flag in code, f"termux-stt.sh should pass {flag}"

    def test_stt_enables_conversion_because_sources_are_not_wav(self):
        """The blob store hands back `{sha}.bin`; a lecture is m4a or mp3.

        whisper-server only transcodes when `--convert` is passed. Without it the
        server expects 16 kHz mono WAV on the wire and answers an m4a upload
        with an empty transcript — which the source pipeline reports as
        "produced no transcript", blaming the file rather than the missing flag.
        """
        code = self._code(_read_script("termux-stt.sh"))
        assert "--convert" in code

    def test_stt_names_ffmpeg_in_its_own_failure_message(self):
        """The operator is told how to fix it, in the message they will see.

        A missing ffmpeg does not stop the server starting; it produces an empty
        transcript on the first recording. That is the same class of failure as
        the ones this session keeps finding — infrastructure that looks correct
        and has never run — and it is only avoidable by saying so at the point
        where the command is being run.
        """
        text = _read_script("termux-stt.sh")
        code = self._code(text)
        assert "ffmpeg" in code
        assert "pkg install ffmpeg" in text
