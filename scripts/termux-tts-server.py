#!/data/data/com.termux/files/usr/bin/python3
"""Local text-to-speech over the OpenAI shape, for the phone.

Why not piper's own server.py
-----------------------------
Three reasons, each disqualifying on a phone rather than merely inconvenient:

1. It binds ``0.0.0.0``. Every other server in this project is loopback-only,
   and on Android that means the synthesis endpoint is reachable from the
   local network with no credential. The project's own rule — the API is
   loopback-only, models hold a phone's RAM — is not a preference here.

2. ``MODELS_DIR`` is hardcoded to ``/app/models``, a Docker path, and the port
   to 9600 via ``app.run(host=..., port=9600)`` with no override. Running it
   unmodified would need the phone's filesystem arranged around a container
   layout that does not exist.

3. It needs Flask, which is another thing to install on the phone, for two
   endpoints and a subprocess call.

So this is the same contract in about a hundred lines and no dependencies beyond
the standard library. It is deliberately *not* a second implementation of
anything the supervisor does — the supervisor still owns start, stop and probe,
and `termux-tts.sh` remains the only place a piper process is identified.

One voice lives elsewhere
-------------------------
The tsukuyomi voice needs piper-plus, which needs onnxruntime - and
onnxruntime's aarch64 wheel needs glibc, so it cannot install in Termux
(bionic), while stock piper cannot run the voice at all ("'espeak' is
not a valid PhonemeType", reproduced with the identical binary on the
PC). So that one voice runs on piper-plus in the proot Ubuntu guest on
:8092, and this server forwards requests for it there, passing the
request body through unchanged. The forward keys on the exact voice
stem, so every other voice keeps resolving to local piper, and the guest
voice is offered in /v1/models only while its backend answers.

What it guarantees
------------------
- Binds 127.0.0.1 unless explicitly told otherwise, and says so on startup.
- Writes to a temporary file and renames on success, so a synthesis killed
  part-way never leaves a truncated WAV where an episode points at it.
- Reports a missing voice as a 404 naming the voice, not as a 500.
- Runs piper under the thread cap it is given, because this server's process
  count is what the residency panel counts.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.getenv("TTS_HOST", "127.0.0.1")
PORT = int(os.getenv("TTS_PORT", "8091"))
MODELS_DIR = os.getenv("TTS_MODELS_DIR", os.path.expanduser("~/piper_models"))
PIPER_BIN = os.getenv("PIPER_BIN", os.path.expanduser("~/piper-build/prefix/bin/piper"))
THREADS = int(os.getenv("TTS_THREADS", "2"))
DEFAULT_VOICE = os.getenv("TTS_MODEL", "en_US-amy-medium")
#: piper reads the whole request from stdin and writes one file. A phone
#: rendering a long podcast segment is slow enough that a client-side timeout
#: is a real possibility, so this is generous rather than tight.
TIMEOUT = int(os.getenv("TTS_TIMEOUT", "900"))

#: The piper-plus server in the proot guest. proot shares Termux's network
#: namespace, so the guest's loopback is this process's loopback.
GUEST_TTS_URL = os.getenv("TTS_GUEST_URL", "http://127.0.0.1:8092")
GUEST_TTS_VOICE = os.getenv("TTS_GUEST_VOICE", "tsukuyomi-chan-6lang-fp16")
#: How long a guest-reachability answer may be reused. Never probing would
#: let /v1/models offer a voice whose backend died minutes ago; probing on
#: every request would put a 2-second floor on each turn while the guest
#: is down. Ten seconds is the middle: the list is fresh enough to trust
#: and the common down-case costs nothing per request.
GUEST_PROBE_TTL = float(os.getenv("TTS_GUEST_PROBE_TTL", "10"))
_probe_at = 0.0
_probe_up = False


def is_guest_voice(name: str | None) -> bool:
    """Whether `name` names the one voice the guest backend serves.

    Matched as the exact stem - with piper's own ``piper-`` prefix
    stripped - never as a prefix, because a prefix match would swallow
    any future local voice whose name starts the same way.
    """
    wanted = (name or "").strip()
    if wanted.startswith("piper-"):
        wanted = wanted[len("piper-") :]
    return os.path.splitext(os.path.basename(wanted))[0] == GUEST_TTS_VOICE


def guest_up() -> bool:
    """Is the guest server answering? Cached for GUEST_PROBE_TTL seconds."""
    global _probe_at, _probe_up
    now = time.monotonic()
    if now - _probe_at < GUEST_PROBE_TTL:
        return _probe_up
    try:
        with closing(
            urllib.request.urlopen(f"{GUEST_TTS_URL}/health", timeout=2)
        ) as response:
            _probe_up = response.status == 200
    except OSError:
        _probe_up = False
    _probe_at = now
    return _probe_up


def voices() -> list[str]:
    local: list[str] = []
    if os.path.isdir(MODELS_DIR):
        local = [
            os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(MODELS_DIR, "*.onnx"))
        ]
    # The guest voice is dropped from the local list even if someone
    # copied its .onnx into MODELS_DIR: stock piper cannot run it, so a
    # local path would be handed out for a synthesis that dies writing a
    # zero-byte WAV - precisely the failure this split exists to end.
    local = [name for name in local if name != GUEST_TTS_VOICE]
    if guest_up() and GUEST_TTS_VOICE not in local:
        local.append(GUEST_TTS_VOICE)
    return sorted(local)


def resolve_voice(name: str | None) -> str | None:
    """Map a requested voice to a file on disk, or None.

    Accepts a bare stem (``en_US-amy-medium``), a filename, or ``piper-<stem>``
    — the last because piper's own server takes that prefix and callers may have
    learned it. Anything unresolvable returns None so the caller can answer 404
    with the name, rather than 500 from a missing file deeper in.
    """
    available = voices()
    if not available:
        return None
    wanted = (name or DEFAULT_VOICE).strip()
    if wanted.startswith("piper-"):
        wanted = wanted[len("piper-") :]
    # do_POST forwards guest voices before reaching here, but a local
    # path for one would still be the wrong answer if a future caller
    # asks directly - so it never resolves.
    if os.path.splitext(os.path.basename(wanted))[0] == GUEST_TTS_VOICE:
        return None
    for stem in available:
        if stem == wanted:
            return os.path.join(MODELS_DIR, f"{stem}.onnx")
    return None


def synthesize(text: str, voice_path: str) -> bytes:
    """Render `text` and return the audio. Raises on any failure."""
    handle, temporary = tempfile.mkstemp(suffix=".wav", prefix="piper-")
    os.close(handle)
    try:
        command = [
            PIPER_BIN,
            "--model",
            voice_path,
            "--output_file",
            temporary,
        ]
        # Only passed when actually set. piper finds its own espeak-ng-data
        # relative to the binary, and passing `--espeak_data ""` overrides that
        # with the process's working directory — so synthesis fails with a
        # phonemisation error that names no file. An empty value is not the
        # same as an absent one.
        espeak_data = os.environ.get("ESPEAK_DATA", "").strip()
        if espeak_data:
            command += ["--espeak_data", espeak_data]
        completed = subprocess.run(
            command,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(
                "piper exited %s: %s"
                % (completed.returncode, detail[-1][:200] if detail else "no output")
            )
        with open(temporary, "rb") as audio:
            data = audio.read()
        if not data:
            raise RuntimeError("piper wrote no audio")
        return data
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "onb-tts/1.0"

    # A synthesis request body is text, which stays small; the *response* is the
    # audio. 32 MiB covers a long podcast segment without letting an unauthenticated
    # caller buffer without bound.
    MAX_BODY = 32 * 1024 * 1024

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client gave up mid-download. Nothing to do and nothing to log:
            # a synthesis that completed is a synthesis that worked.
            pass

    def _json(self, code: int, payload: object) -> None:
        self._send(
            code,
            json.dumps(payload).encode("utf-8"),
            "application/json",
        )

    def _forward(self, raw: bytes) -> None:
        """Pass the request to the guest piper-plus server, verbatim.

        The guest's own answers pass through with their status and body,
        so a caller sees "model not present" rather than a generic proxy
        failure. Only an unreachable guest is this server's error to
        report - the voice is listed only while the guest answers, but
        the two are ten seconds apart by design, so a race lands here.
        """
        request = urllib.request.Request(
            f"{GUEST_TTS_URL}/v1/audio/speech",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with closing(urllib.request.urlopen(request, timeout=TIMEOUT)) as response:
                content_type = response.headers.get("Content-Type") or "audio/wav"
                self._send(response.status, response.read(), content_type)
        except urllib.error.HTTPError as exc:
            # Ordered before OSError below: HTTPError is an OSError, and
            # its body is the guest's explanation, which must survive.
            self._send(exc.code, exc.read(), "application/json")
        except OSError as exc:
            self._json(
                503,
                {
                    "error": {
                        "message": (
                            f"guest TTS at {GUEST_TTS_URL} did not answer ({exc}); "
                            "start it with: onb-stack up"
                        )
                    }
                },
            )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "owned_by": "piper"}
                        for name in voices()
                    ],
                },
            )
            return
        if self.path.rstrip("/") in ("/health", ""):
            # guest_tts lets a single probe show whether the second
            # runtime is beside this one - `onb-stack status` and the PC
            # panel both read it rather than guessing from a dead voice.
            self._json(
                200,
                {
                    "status": "ok",
                    "voices": len(voices()),
                    "guest_tts": "up" if guest_up() else "down",
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

        requested = str(payload.get("voice") or payload.get("model") or "")
        if is_guest_voice(requested):
            # The body passes through unchanged - the same OpenAI shape
            # both servers speak - so the two contracts cannot drift.
            self._forward(raw)
            return
        voice_path = resolve_voice(requested or None)
        if voice_path is None:
            # Named, so the caller can see which voice it asked for. A generic
            # 500 here would read as "TTS is broken" when it is "that voice is
            # not installed", which is a different fix.
            self._json(
                404,
                {
                    "error": {
                        "message": f"voice {requested or DEFAULT_VOICE!r} is not installed",
                        "available": voices(),
                    }
                },
            )
            return

        try:
            audio = synthesize(text, voice_path)
        except subprocess.TimeoutExpired:
            self._json(504, {"error": {"message": f"piper exceeded {TIMEOUT}s"}})
            return
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            print(f"[tts] {exc}", file=sys.stderr, flush=True)
            self._json(500, {"error": {"message": str(exc)[:200]}})
            return

        self._send(200, audio, "audio/wav")

    def log_message(self, fmt: str, *args) -> None:
        # Default logging is one line per request to stderr, which on a phone
        # fills the termux buffer within a day. Errors are printed explicitly
        # where they matter.
        pass


def main() -> int:
    if not os.path.isfile(PIPER_BIN):
        print(
            f"[tts] piper binary not found at {PIPER_BIN}. "
            "Build it with: sh ~/bin/onb-build-piper",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(
        f"[tts] serving on {HOST}:{PORT} from {MODELS_DIR} "
        f"({len(voices())} voice(s)), piper at {PIPER_BIN}, threads={THREADS}",
        file=sys.stderr,
        flush=True,
    )
    if HOST != "127.0.0.1":
        # Said loudly and said at startup, because the default is loopback and
        # anything else is a decision someone should have made deliberately.
        print(
            f"[tts] WARNING: bound to {HOST}, not loopback. Anyone who can reach "
            "this address can use the phone to synthesise speech.",
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
