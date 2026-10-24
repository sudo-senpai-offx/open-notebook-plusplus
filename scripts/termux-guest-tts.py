#!/usr/bin/env python3
"""The piper-plus TTS server, for the proot Ubuntu guest. Port 8092, loopback.

Why this exists - the two-runtime split, settled by measurement:

* stock piper (Termux, :8091) cannot run espeak-type voices: piper 1.2.0
  fails tsukuyomi/Kokoro/amy with "'espeak' is not a valid PhonemeType",
  reproduced on the PC with the identical binary.
* piper-plus runs those voices, but needs onnxruntime, whose manylinux
  aarch64 wheel needs glibc - there is no Termux/bionic wheel at all.
* the proot guest IS glibc, so piper-plus installs there, and proot shares
  the network namespace, so 127.0.0.1:8092 is visible from Termux.

The Termux :8091 server forwards tsukuyomi-voice requests here; the app
never learns there are two runtimes.

Same contract as :8091 - loopback-only, OpenAI shape (/v1/audio/speech,
/v1/models, /health), standard-library HTTP only. piper-plus is the one
dependency and it is imported on the first synthesis (ADR-014: models load
on demand, and that import pulls onnxruntime), so the process boots cheap
and a phone that never speaks Japanese pays nothing for this server.

Threads: PIPER_INTRA_THREADS is set before piper is imported, and
piper-plus clamps it to at most 4 and always runs inter-op at 1 - so this
server cannot take the whole CPU however many cores the guest can see.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.getenv("GUEST_TTS_HOST", "127.0.0.1")
PORT = int(os.getenv("GUEST_TTS_PORT", "8092"))
MODELS_DIR = os.getenv("GUEST_TTS_MODELS_DIR", "/root/piper_models")
MODEL = os.getenv("GUEST_TTS_MODEL", "tsukuyomi-chan-6lang-fp16")
#: The phone's media-class inference allowance, the same number the Termux
#: piper server defaults to. piper-plus reads this (clamped to <=4) at model
#: load, so it is set here, before that import can happen.
THREADS = int(os.getenv("GUEST_TTS_THREADS", "2"))
TIMEOUT = int(os.getenv("GUEST_TTS_TIMEOUT", "900"))
NLTK_DATA = os.getenv("GUEST_TTS_NLTK_DATA", "/root/nltk_data")

os.environ.setdefault("PIPER_INTRA_THREADS", str(THREADS))
os.environ.setdefault("NLTK_DATA", NLTK_DATA)

MODEL_PATH = os.path.join(MODELS_DIR, f"{MODEL}.onnx")
CONFIG_PATH = f"{MODEL_PATH}.json"

#: The same script-detection server_ja.py proved on the PC: kana is
#: Japanese, CJK ideographs are Chinese, Latin is English, and anything
#: else falls back to Japanese (the model's own language).
_RE_KANA = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u31F0-\u31FF]")
_RE_CJK = re.compile(r"[\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF]")
_RE_LATIN = re.compile(r"[A-Za-z\xC0-\xFF]")
#: language_id_map from the model config, inlined because piper is not
#: imported until first use and this must work before that.
_LANG_ID = {"ja": 0, "en": 1, "zh": 2, "es": 3, "fr": 4, "pt": 5}

_LOCK = threading.Lock()
#: One utterance at a time. The Termux server gets this for free - each
#: request is a piper subprocess - and in-process it has to be taken on
#: purpose: a phone should not run two syntheses against the same session
#: while llama-server is beside it.
_SYNTH_LOCK = threading.Lock()
_VOICE = None


def detect_language(text: str) -> int:
    """language_id for the dominant script in `text`."""
    if _RE_KANA.search(text):
        return _LANG_ID["ja"]
    if _RE_CJK.search(text):
        return _LANG_ID["zh"]
    if _RE_LATIN.search(text):
        return _LANG_ID["en"]
    return _LANG_ID["ja"]


def voice():
    """Load the model on first use, once (ADR-014). Raises on any failure."""
    global _VOICE
    if _VOICE is not None:
        return _VOICE
    with _LOCK:
        if _VOICE is None:
            # Deferred: the import chain pulls onnxruntime and the
            # pyopenjtalk dictionary, which a boot that serves nothing
            # should not pay.
            from piper.voice import PiperVoice  # noqa: PLC0415

            _VOICE = PiperVoice.load(MODEL_PATH, CONFIG_PATH)
    return _VOICE


def synthesize(text: str) -> bytes:
    """Render `text` to WAV bytes, in memory. Raises on any failure."""
    target = voice()
    with _SYNTH_LOCK:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            target.synthesize(text, wav_file, language_id=detect_language(text))
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "onb-guest-tts/1.0"

    # Same bound as the Termux server: the request body is text, the
    # response is the audio, and neither grows without limit.
    MAX_BODY = 32 * 1024 * 1024

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: object) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL, "object": "model", "owned_by": "piper-plus"}
                    ],
                },
            )
            return
        if self.path.rstrip("/") in ("/health", ""):
            # model_present distinguishes "not installed" from "installed
            # but not loaded", so `onb-stack status` can say which fix to
            # apply instead of one vague DOWN.
            self._json(
                200,
                {
                    "status": "ok",
                    "model": MODEL,
                    "model_present": os.path.isfile(MODEL_PATH),
                    "loaded": _VOICE is not None,
                    "threads": THREADS,
                },
            )
            return
        self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/audio/speech", "/audio/speech"):
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": {"message": "empty body"}})
            return
        if length > self.MAX_BODY:
            self._json(413, {"error": {"message": "request too large"}})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": {"message": "body is not JSON"}})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": {"message": "body is not a JSON object"}})
            return

        text = str(payload.get("input") or payload.get("text") or "").strip()
        if not text:
            self._json(400, {"error": {"message": "no text to speak"}})
            return

        started = time.monotonic()
        try:
            audio = synthesize(text)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            print(f"[guest-tts] {exc}", file=sys.stderr, flush=True)
            self._json(
                500,
                {
                    "error": {
                        "message": str(exc)[:200],
                        "model_present": os.path.isfile(MODEL_PATH),
                    }
                },
            )
            return
        took = time.monotonic() - started
        print(
            f"[guest-tts] {len(text)} chars -> {len(audio)} bytes in {took:.1f}s",
            file=sys.stderr,
            flush=True,
        )
        self._send(200, audio, "audio/wav")

    def log_message(self, fmt: str, *args) -> None:
        # One stderr line per request fills the log within a day, exactly as
        # on the Termux server. Synthesis results are printed explicitly.
        pass


def main() -> int:
    if not os.path.isfile(MODEL_PATH):
        print(
            f"[guest-tts] model not present at {MODEL_PATH}. "
            "Install with: onb-tts-ja-install (from Termux)",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        f"[guest-tts] serving on {HOST}:{PORT}, model {MODEL_PATH}, "
        f"threads={THREADS} (intra) 1 (inter), piper-plus on first request",
        file=sys.stderr,
        flush=True,
    )
    if HOST != "127.0.0.1":
        print(
            f"[guest-tts] WARNING: bound to {HOST}, not loopback. Anyone who "
            "can reach this address can use the phone to synthesise speech.",
            file=sys.stderr,
            flush=True,
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
