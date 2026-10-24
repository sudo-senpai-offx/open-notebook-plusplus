"""The two-runtime TTS split: the :8091 forward rule and the guest server.

Why the split exists (measured, not guessed): stock piper 1.2.0 cannot run
espeak-type voices - "'espeak' is not a valid PhonemeType", reproduced with
the identical binary on the PC - and piper-plus, which can, needs
onnxruntime, whose manylinux aarch64 wheel needs glibc and has no
Termux/bionic wheel at all. So the tsukuyomi voice runs on piper-plus in
the proot guest (:8092) and the Termux server forwards to it.

What is under test here is the boundary between the two runtimes:

* requests for the guest voice are forwarded body-unchanged, and every
  other voice stays on the local piper path;
* the guest voice is listed only while its backend answers, and stock
  piper is never handed its file - that path produced zero-byte WAVs;
* the guest server boots without piper installed (its module-level load
  in this file *is* that proof: piper-plus is not in this environment, so
  a top-level import would fail at collection) and reports an absent
  model honestly rather than as generic breakage.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
GUEST_VOICE = "tsukuyomi-chan-6lang-fp16"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Imported here rather than subprocessed: both servers are stdlib-only at
# module level, so loading them IS the dependency check for each.
termux_tts = _load("termux-tts-server")
guest_tts = _load("termux-guest-tts")


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch: Any) -> None:
    # The reachability cache must not leak between tests: a True cached by
    # one test would list the guest voice in the next.
    monkeypatch.setattr(termux_tts, "_probe_at", 0.0)
    monkeypatch.setattr(termux_tts, "_probe_up", False)


@pytest.fixture
def serve() -> Iterator[Callable[[Any], ThreadingHTTPServer]]:
    running: list[ThreadingHTTPServer] = []

    def _start(module: Any) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        running.append(server)
        return server

    yield _start
    for server in running:
        server.shutdown()
        server.server_close()


def _url(server: ThreadingHTTPServer, path: str) -> str:
    return f"http://127.0.0.1:{server.server_port}{path}"


def _get(server: ThreadingHTTPServer, path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(_url(server, path), timeout=5) as response:
        return response.status, response.read()


def _post(
    server: ThreadingHTTPServer, path: str, payload: dict[str, Any]
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        _url(server, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestForwardRouting:
    """Requests are routed by exact voice stem, and nothing else moves."""

    @pytest.mark.parametrize(
        "name",
        [
            GUEST_VOICE,
            f"piper-{GUEST_VOICE}",
            f"/root/piper_models/{GUEST_VOICE}.onnx",
        ],
    )
    def test_the_guest_voice_is_recognised(self, name: str) -> None:
        assert termux_tts.is_guest_voice(name) is True

    @pytest.mark.parametrize(
        "name",
        # "tsukuyomi" alone must not match: a prefix rule would swallow any
        # future local voice whose name starts the same way.
        [
            "en_US-amy-medium",
            "zh_CN-huayan-medium",
            "tsukuyomi",
            "tsukuyomi-chan",
            None,
            "",
            "  ",
        ],
    )
    def test_everything_else_stays_local(self, name: str | None) -> None:
        assert termux_tts.is_guest_voice(name) is False

    def test_a_local_voice_gets_the_local_answer_not_a_forward(
        self, serve: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        # No models installed, so the honest answer is 404-with-names -
        # which the guest server could not produce. Reaching the guest here
        # would show up as its 500, not this 404.
        monkeypatch.setattr(termux_tts, "MODELS_DIR", str(tmp_path))
        monkeypatch.setattr(termux_tts, "guest_up", lambda: False)
        server = serve(termux_tts)
        status, body = _post(
            server,
            "/v1/audio/speech",
            {"voice": "en_US-amy-medium", "input": "hello"},
        )
        assert status == 404
        assert b"not installed" in body


class TestGuestVoiceListing:
    """The voice appears only while its backend can actually serve it."""

    def test_the_local_list_never_contains_the_guest_voice(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        # The file is physically present: stock piper would be handed it
        # and emit a zero-byte WAV - the exact failure this split ends.
        (tmp_path / "en_US-amy-medium.onnx").write_bytes(b"not really onnx")
        (tmp_path / f"{GUEST_VOICE}.onnx").write_bytes(b"not really onnx")
        monkeypatch.setattr(termux_tts, "MODELS_DIR", str(tmp_path))
        monkeypatch.setattr(termux_tts, "guest_up", lambda: False)
        assert termux_tts.voices() == ["en_US-amy-medium"]

    def test_it_is_offered_while_the_backend_answers(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(termux_tts, "MODELS_DIR", str(tmp_path))
        monkeypatch.setattr(termux_tts, "guest_up", lambda: True)
        assert termux_tts.voices() == [GUEST_VOICE]

    def test_stock_piper_never_resolves_its_file(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(termux_tts, "MODELS_DIR", str(tmp_path))
        monkeypatch.setattr(termux_tts, "guest_up", lambda: True)
        assert termux_tts.resolve_voice(GUEST_VOICE) is None

    def test_health_says_whether_the_guest_is_up(
        self, serve: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(termux_tts, "MODELS_DIR", str(tmp_path))
        monkeypatch.setattr(termux_tts, "guest_up", lambda: True)
        server = serve(termux_tts)
        status, body = _get(server, "/health")
        assert status == 200
        assert json.loads(body)["guest_tts"] == "up"


class TestReachabilityProbe:
    """The probe answers "is the backend there" without lying either way."""

    def test_a_dead_backend_answers_down(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(termux_tts, "GUEST_TTS_URL", "http://127.0.0.1:1")
        assert termux_tts.guest_up() is False
        assert termux_tts._probe_up is False

    def test_a_fresh_answer_is_reused_without_probing(self, monkeypatch: Any) -> None:
        # Cached True within the TTL: the answer survives even though the
        # URL is now dead - which is the whole point of the cache window.
        monkeypatch.setattr(termux_tts, "GUEST_TTS_URL", "http://127.0.0.1:1")
        monkeypatch.setattr(termux_tts, "_probe_at", time.monotonic())
        monkeypatch.setattr(termux_tts, "_probe_up", True)
        assert termux_tts.guest_up() is True


class TestForwardingToTheGuest:
    """The forward itself: verbatim body, guest's errors, dead backend."""

    def test_the_guests_own_answer_passes_back_with_its_status(
        self, serve: Any, monkeypatch: Any
    ) -> None:
        guest = serve(guest_tts)
        monkeypatch.setattr(termux_tts, "GUEST_TTS_URL", _url(guest, ""))
        monkeypatch.setattr(guest_tts, "MODEL_PATH", "/nonexistent/absent.onnx")
        server = serve(termux_tts)
        status, body = _post(
            server,
            "/v1/audio/speech",
            {"voice": GUEST_VOICE, "input": "こんにちは"},
        )
        # The guest has no model on a test host, so its real answer - 500
        # with model_present False - must arrive intact through the proxy.
        assert status == 500
        error = json.loads(body)["error"]
        assert error["model_present"] is False

    def test_an_unreachable_backend_is_503_with_an_actionable_message(
        self, serve: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(termux_tts, "GUEST_TTS_URL", "http://127.0.0.1:1")
        server = serve(termux_tts)
        status, body = _post(
            server,
            "/v1/audio/speech",
            {"voice": GUEST_VOICE, "input": "hello"},
        )
        assert status == 503
        message = json.loads(body)["error"]["message"]
        assert "did not answer" in message
        assert "onb-stack up" in message


class TestTheGuestServer:
    """The :8092 server: cheap boot, honest failure, one voice."""

    def test_it_imported_without_piper(self) -> None:
        # The module-level load above ran in this environment, where
        # piper-plus is not installed: a top-level import would have
        # failed at collection. Lazy means the phone pays only on use.
        assert guest_tts._VOICE is None

    def test_health_distinguishes_absent_from_unloaded(
        self, serve: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(guest_tts, "MODEL_PATH", "/nonexistent/absent.onnx")
        server = serve(guest_tts)
        status, body = _get(server, "/health")
        payload = json.loads(body)
        assert status == 200
        assert payload["model_present"] is False
        assert payload["loaded"] is False
        assert payload["threads"] == 2

    def test_the_models_list_is_the_one_voice(self, serve: Any) -> None:
        server = serve(guest_tts)
        status, body = _get(server, "/v1/models")
        assert status == 200
        assert json.loads(body)["data"][0]["id"] == GUEST_VOICE

    def test_speech_without_text_is_400(self, serve: Any) -> None:
        server = serve(guest_tts)
        status, _body = _post(server, "/v1/audio/speech", {"voice": GUEST_VOICE})
        assert status == 400

    def test_a_failed_load_is_500_that_says_which_model(
        self, serve: Any, monkeypatch: Any
    ) -> None:
        # Not a generic 500: the caller can see the model was the problem
        # and that installing it is the fix.
        monkeypatch.setattr(guest_tts, "MODEL_PATH", "/nonexistent/absent.onnx")
        server = serve(guest_tts)
        status, body = _post(server, "/v1/audio/speech", {"input": "こんにちは"})
        assert status == 500
        error = json.loads(body)["error"]
        assert error["model_present"] is False
        assert error["message"]

    @pytest.mark.parametrize(
        "text, want",
        [
            ("こんにちは、今日は良い天気ですね。", 0),  # kana -> ja
            ("你好世界", 2),  # ideographs -> zh
            ("Hello, how are you today?", 1),  # latin -> en
            ("12345 !!!", 0),  # digits/punct -> the model's own language
            ("今日はgood morningですね", 0),  # mixed: kana wins
        ],
    )
    def test_language_detection_follows_the_proven_rules(
        self, text: str, want: int
    ) -> None:
        assert guest_tts.detect_language(text) == want
