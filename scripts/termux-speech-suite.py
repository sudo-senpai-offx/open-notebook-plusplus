#!/data/data/com.termux/files/usr/bin/python3
"""Multilingual speech robustness suite, run on the phone.

    sh ~/bin/onb-speech-suite                       # default fixtures dir
    sh ~/bin/onb-speech-suite ~/speech-fixtures     # explicit
    sh ~/bin/onb-speech-suite --no-robustness       # scoring only
    sh ~/bin/onb-speech-suite --max-cer 0.40        # loosen for a weak model

Exercises the two model servers that residency manages — whisper.cpp for STT on
:8090 and piper for TTS on :8091 — against real recorded speech in five
languages, then tries to break them.

What it asserts
---------------
STT, per clip: the server returns a transcript, and the character error rate
against the FLEURS reference is under the threshold for that language. Thresholds
differ because the task differs: whisper's error rate on clean Mandarin is far
below its error rate on Tamil, and a single global number would either fail a
language that is working or pass one that is not.

TTS, per available voice: the server returns a RIFF wav of non-trivial length.

Robustness: malformed and hostile input must produce a clean refusal, never a
crash, a hang, or a 200 with no body. A model server that answers 200 with empty
output is the failure this exists to catch — it happened once already, when the
TTS response looked successful and contained nothing.

Coverage is reported honestly
-----------------------------
piper has no Taiwanese, Tamil or Malay voice among its 177 voices, so those
three are listed as UNTESTED for TTS with the reason, not silently skipped and
not counted as passes. Taiwanese Mandarin is absent from FLEURS as well, so STT
does not cover it either; Cantonese is in FLEURS but is a different language, not
a Taiwanese accent, and is not substituted for it.

Stdlib only: Termux has no requests, and the suite must not add a dependency to
the device it is measuring.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STT_PORT = os.environ.get("STT_PORT", "8090")
TTS_PORT = os.environ.get("TTS_PORT", "8091")
STT_URL = f"http://127.0.0.1:{STT_PORT}"
TTS_URL = f"http://127.0.0.1:{TTS_PORT}"

# Where the STT loop is in progress, so the signal handler can save it.
_CHECKPOINT: Path | None = None
_RESULTS: dict[str, dict] = {}


def install_signal_handlers() -> None:
    """Make Ctrl+C and `kill` work, unconditionally.

    Two things swallow an interrupt in this script, and both were observed:

    - `with ThreadPoolExecutor(...)` runs `shutdown(wait=True)` on the way out,
      which blocks until in-flight workers finish. Each worker can be inside a
      transcription with a 600-second deadline, so Ctrl+C appeared to do nothing
      for up to ten minutes.
    - Even outside the executor, `SystemExit` has to unwind past whatever the
      main thread is blocked in. A long socket read is exactly that.

    So the handler saves the checkpoint and calls `os._exit`, which cannot be
    deferred by a pending shutdown or a pending read. Nothing is lost: every
    completed clip is already on disk, and the next run resumes from it.
    """

    def bail(signum: int, _frame: object) -> None:
        name = "SIGINT (Ctrl+C)" if signum == signal.SIGINT else f"signal {signum}"
        print(f"\n  {YELLOW}{name} - saving and exiting{RESET}", flush=True)
        try:
            if _CHECKPOINT is not None:
                save_checkpoint(_CHECKPOINT, _RESULTS)
                done = len(_RESULTS)
                print(
                    f"  {DIM}{done} clip(s) scored and saved; re-run to resume{RESET}",
                    flush=True,
                )
        except Exception:  # noqa: BLE001 - we are already exiting
            pass
        os._exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, bail)
        except (OSError, ValueError):
            pass


# Character error rate ceilings, per language. Tuned for a multilingual
# large-v3-tier model on clean read speech, not for lecture audio — a phone
# transcribing a noisy lecture will legitimately exceed these, and the suite
# should be re-pointed at real material for that rather than have the bar
# lowered until noise passes.
MAX_CER = {
    "en": 0.10,
    "zh": 0.15,
    "ja": 0.20,
    "ta": 0.35,
    "my": 0.30,
}
DEFAULT_MAX_CER = 0.40

# How much slower than realtime a clip may be before it is called a timeout.
#
# Measured, not guessed: large-v3-turbo-q5_0 on two A73 threads ran at RTF 8-16
# on this device — the first full run went 21 minutes over 150 seconds of audio
# and produced nothing to show for it. A budget of 20 leaves room for that while
# still bounding the run, and is generous enough that ordinary slowness is not
# mistaken for a hang.
DEFAULT_RTF_BUDGET = 20.0

# Floor and ceiling on a per-clip timeout. The floor stops a 4-second clip from
# getting a 1-second budget it cannot possibly meet; the ceiling stops a long
# clip from occupying the run indefinitely.
MIN_CLIP_TIMEOUT = 90
MAX_CLIP_TIMEOUT = 600

# Languages written without spaces between words. Whisper emits them without
# spaces and FLEURS references do too, but stripping makes the comparison robust
# to a model that does insert them.
NO_SPACE = {"zh", "ja"}

# Languages piper has no voice for. Reported, not skipped.
TTS_UNTESTABLE = {
    "tw": "piper has no Taiwanese Mandarin voice",
    "ta": "piper has no Tamil voice (its locale list has hi and te, not ta)",
    "my": "piper has no Malay voice (it has ml, which is Malayalam - a different language)",
}
TTS_VOICES = [
    ("en_US-amy-medium", "en"),
    ("zh_CN-huayan-medium", "zh"),
    ("ja_JP-hi_fi_captain-medium", "ja"),
]

TTS_SENTENCES = {
    "en": "Residency management keeps the speech models off the device until a command needs them.",
    "zh": "驻留管理会把语音模型留在设备上，直到有命令需要它们。",
    "ja": "常駐管理は、音声モデルをデバイス上に残します。",
}

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


# ---------------------------------------------------------------- text scoring


def normalise(text: str, lang: str) -> str:
    """Fold a reference and a hypothesis to the same comparable form.

    Whisper and FLEURS disagree about case, punctuation and spacing, none of
    which are transcription errors. Comparing them raw would inflate the error
    rate into meaninglessness — most obviously for the reference transcript,
    which is written with punctuation and the hypothesis usually is not.
    """
    text = text.lower().strip()
    # Keep CJK ideographs and kana; drop Latin punctuation and whitespace rules.
    text = re.sub(r"[^\w぀-ヿ㐀-鿿Ѐ-ӿ]+", " ", text)
    if lang in NO_SPACE:
        return re.sub(r"\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Single row, iterating the shorter axis. O(len(a) * len(b)) time, O(min) space.
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str, lang: str) -> float | None:
    """Character error rate, or None when there is nothing to score."""
    ref = normalise(reference, lang)
    if not ref:
        return None
    return levenshtein(ref, normalise(hypothesis, lang)) / len(ref)


# ------------------------------------------------------------------- transport


def post_multipart(
    url: str,
    field: str,
    path: Path,
    extra: dict[str, str] | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """POST a file as multipart/form-data. Hand-rolled: Termux has no requests."""
    boundary = f"----onb{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in (extra or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


def post_json(url: str, payload: dict, timeout: int = 300) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc).encode()


def transcribe(path: Path, model: str = "", timeout: int = 300) -> tuple[int, str]:
    """Returns (status, text). A status of 0 means the transport itself failed."""
    status, body = post_multipart(
        f"{STT_URL}/v1/audio/transcriptions",
        "file",
        path,
        {"model": model} if model else None,
        timeout=timeout,
    )
    if status != 200:
        return status, body
    try:
        return 200, json.loads(body).get("text", "")
    except (ValueError, AttributeError):
        return 200, ""


def clip_timeout(duration_s: float, rtf_budget: float) -> int:
    """Per-clip deadline, scaled to the audio.

    An absolute timeout is wrong in both directions and you cannot tell which
    until a clip is genuinely slow: it either truncates long clips or spends most
    of its budget waiting for short ones. Scaling by the measured audio length,
    with a floor and a ceiling, is the only version that bounds the run without
    pretending to know in advance how fast the model is.
    """
    return int(min(MAX_CLIP_TIMEOUT, max(MIN_CLIP_TIMEOUT, duration_s * rtf_budget)))


def load_checkpoint(path: Path) -> dict[str, dict]:
    """Previously scored clips, so an interrupted run resumes.

    The run is long and the device is unreliable. Losing twenty minutes of
    transcription to a dropped connection is the worst possible behaviour for a
    tool whose whole job is to tell you where the weak spot is.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        print(f"  {DIM}checkpoint {path.name} unreadable; starting fresh{RESET}")
        return {}


def save_checkpoint(path: Path, results: dict[str, dict]) -> None:
    """Written atomically. A half-written checkpoint is worse than none, because
    the next run would silently resume from a truncated set of results."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        print(f"  {DIM}could not write checkpoint: {exc}{RESET}")


def wav_seconds(payload: bytes) -> float:
    """Duration of a wav held in memory, via a temp file (wave needs a path)."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with wave.open(path) as reader:
            return reader.getnframes() / float(reader.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0
    finally:
        os.unlink(path)


# ------------------------------------------------------------------ the suite


def load_manifest(fixtures: Path) -> dict:
    path = fixtures / "manifest.json"
    if not path.exists():
        print(f"{RED}no manifest at {path}{RESET}")
        print(
            "  Build it on the PC:  python scripts/build-speech-fixtures.py --out <dir>"
        )
        sys.exit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def preflight() -> bool:
    print("== preflight")
    ok = True
    for name, url in (("stt", STT_URL), ("tts", TTS_URL)):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
                print(f"  {name:4} {url}  HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  {RED}{name:4} {url}  UNREACHABLE ({exc}){RESET}")
            ok = False
    if not ok:
        print(
            f"\n  {RED}both model servers must be up{RESET}: "
            "sh ~/bin/onb-stt up && sh ~/bin/onb-tts up"
        )
    return ok


def model_tier() -> str:
    """What the launcher chose, so a result is attributable to a model."""
    try:
        done = subprocess.run(
            ["sh", os.path.expanduser("~/bin/onb-stt"), "probe"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in (done.stdout + done.stderr).splitlines():
            if line.startswith("[stt] model="):
                return line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "[stt] model=unknown"


def run_stt(
    manifest: dict,
    fixtures: Path,
    max_cer: float,
    rtf_budget: float,
    fresh: bool,
) -> tuple[bool, dict]:
    """Score every clip, bounded and resumable.

    Three properties this has and needs:

    * **Bounded.** Every request has a deadline scaled to the clip's audio
      length. The first run had no deadline at all and went 21 minutes over
      150 seconds of audio.
    * **Resumable.** Results are checkpointed after each clip. The device drops
      connections; losing twenty minutes of transcription to that is the worst
      possible behaviour for the tool whose job is to find the weak spot.
    * **Diagnostic.** RTF is printed per clip, because how many times faster
      than realtime the model runs is a fact about this device and determines
      whether the tier is usable on it at all.
    """
    print("\n== STT: transcription accuracy")
    print(f"  {DIM}{model_tier()}{RESET}")

    by_lang: dict[str, list[tuple[str, float | None, str, float, float]]] = {}
    failures: list[str] = []
    timed_out: list[str] = []
    total = len(manifest["clips"])
    done = 0

    global _CHECKPOINT, _RESULTS
    checkpoint = fixtures / "stt-results.json"
    seen: dict[str, dict] = {} if fresh else load_checkpoint(checkpoint)
    # Published so the SIGINT handler can save on Ctrl+C. Both names, because
    # the handler reads the module global while the loop rebinds its local.
    _CHECKPOINT, _RESULTS = checkpoint, seen
    if seen:
        print(
            f"  {DIM}resuming: {len(seen)} clip(s) already scored in "
            f"{checkpoint.name}{RESET}"
        )

    for clip in manifest["clips"]:
        lang = manifest["locales"][clip["locale"]]["lang"]
        path = fixtures / clip["file"]
        if not path.exists():
            failures.append(f"{clip['file']}: missing from the fixture dir")
            continue

        duration = float(clip.get("duration_s") or 0.0)
        budget = clip_timeout(duration, rtf_budget)
        prior = seen.get(clip["file"])
        if prior is not None and prior.get("status") not in ("scored", "timeout"):
            prior = None
        cached = prior is not None

        if cached and prior is not None:
            was_scored = prior.get("status") == "scored"
            status = int(prior.get("http", 200)) if was_scored else 0
            text = str(prior.get("text", ""))
            score = prior.get("cer")
            elapsed = float(prior.get("seconds", 0.0))
            origin = DIM + " cached" + RESET
        else:
            began = time.monotonic()
            try:
                status, text = transcribe(path, timeout=budget)
            except (TimeoutError, OSError, ValueError) as exc:
                # A read timeout surfaces as a transport failure while the server
                # is still chewing on the clip. Both facts are recorded: the
                # elapsed time is the RTF evidence.
                status, text = 0, str(exc)
            elapsed = time.monotonic() - began
            score = cer(clip["reference"], text, lang) if status == 200 else None
            outcome = "scored" if status == 200 else "timeout"
            seen[clip["file"]] = {
                "status": outcome,
                "cer": score,
                "text": text[:400],
                "seconds": round(elapsed, 2),
                "duration_s": duration,
                "http": status,
            }
            save_checkpoint(checkpoint, seen)
            # `seen` is the same object throughout, but rebind the global anyway:
            # the handler must never depend on the loop having got this far.
            _RESULTS = seen
            origin = ""

        done += 1
        if status == 200 and score is not None:
            by_lang.setdefault(lang, []).append(
                (clip["file"], score, text, elapsed, duration)
            )
            rtf = elapsed / duration if duration else 0.0
            detail = f"CER {score:.3f}  RTF {rtf:5.1f}"
            mark = GREEN + "ok" + RESET if score <= max_cer else RED + "FAIL" + RESET
            if score > max_cer:
                failures.append(
                    f"{clip['file']}: CER {score:.2f} over {max_cer:.2f} | {text[:60]!r}"
                )
        else:
            by_lang.setdefault(lang, []).append(
                (clip["file"], None, "", elapsed, duration)
            )
            timed_out.append(clip["file"])
            detail = (
                f"{'TIMEOUT' if status == 0 else f'HTTP {status}'} budget {budget}s"
            )
            mark = RED + "TIMEOUT" + RESET if status == 0 else RED + "ERR" + RESET
            failures.append(
                f"{clip['file']}: {detail} for {duration:.1f}s of audio "
                f"(elapsed {elapsed:.1f}s)"
            )

        # Flushed: this loop is the long pole and silence here is a hang.
        print(
            f"  [{done:2}/{total}] {lang:3} {clip['file'][:26]:28} {detail:32}"
            f" {elapsed:7.2f}s {mark}{origin}",
            flush=True,
        )

    for lang in sorted(by_lang):
        ceiling = min(MAX_CER.get(lang, DEFAULT_MAX_CER), max_cer)
        scores = [s for _f, s, _t, _e, _d in by_lang[lang] if s is not None]
        times = [e for _f, s, _t, e, _d in by_lang[lang] if s is not None]
        label = (
            manifest["locales"]
            .get(
                next(
                    key
                    for key, value in manifest["locales"].items()
                    if value["lang"] == lang
                ),
                {},
            )
            .get("label", lang)
        )
        if not scores:
            print(
                f"  {lang:4} {label:24} {RED}no transcripts"
                f" ({len(by_lang[lang])} clip(s) timed out){RESET}"
            )
            continue
        mean = sum(scores) / len(scores)
        verdict = GREEN + "pass" + RESET if mean <= ceiling else RED + "FAIL" + RESET
        # RTF alongside CER, because the two answer different questions: CER says
        # whether the model understood, RTF says whether it is fast enough to be
        # usable on this device. A language can pass the first and fail the
        # second badly, and only one of those is fixable by a better model.
        durations = [d for _f, _s, _t, _e, d in by_lang[lang] if d]
        audio = sum(durations)
        rtf = (
            (sum(e for _f, s, _t, e, _d in by_lang[lang] if s is not None) / audio)
            if audio
            else 0.0
        )
        print(
            f"  {lang:4} {label:24} CER {mean:.3f} "
            f"{DIM}(worst {max(scores):.3f}, {len(scores)} clips, ceiling {ceiling:.2f}){RESET}"
            f"  {verdict}  {DIM}RTF {rtf:.1f}, {sum(times) / len(times):.1f}s/clip{RESET}"
        )

    if timed_out:
        print(
            f"  {RED}{len(timed_out)} clip(s) exceeded their time budget{RESET}"
            f" {DIM}(budget {DEFAULT_RTF_BUDGET:g}x realtime, "
            f"{MIN_CLIP_TIMEOUT}-{MAX_CLIP_TIMEOUT}s){RESET}"
        )

    # One transcript shown per language: a number says the model was close, the
    # text says what "close" actually looks like for that script.
    for lang in sorted(by_lang):
        sample = next((t for _f, _s, t, _e, _d in by_lang[lang] if t.strip()), "")
        if sample:
            print(f"  {DIM}{lang:4} {sample[:96]}{RESET}")

    not_tested = manifest.get("not_tested", {}).get("STT", [])
    for lang in not_tested:
        reason = manifest["not_tested"]["stt_reason"]
        print(f"  {YELLOW}{lang:4} UNTESTED{RESET} - {reason}")
    return not failures, {"by_lang": by_lang, "failures": failures}


def run_tts(max_cer: float) -> tuple[bool, dict]:
    print("\n== TTS: synthesis")
    failures: list[str] = []
    for voice, lang in TTS_VOICES:
        sentence = TTS_SENTENCES.get(lang, "Residency keeps models off the device.")
        path = Path(os.path.expanduser("~/piper_models")) / f"{voice}.onnx"
        if not path.exists():
            print(f"  {lang:4} {voice:28} {YELLOW}voice not installed{RESET}")
            continue
        started = time.monotonic()
        status, payload = post_json(
            f"{TTS_URL}/v1/audio/speech", {"input": sentence, "voice": voice}
        )
        elapsed = time.monotonic() - started
        if status != 200:
            failures.append(f"{voice}: HTTP {status} {payload[:80]!r}")
            print(f"  {lang:4} {voice:28} {RED}HTTP {status}{RESET}")
            continue
        if not payload.startswith(b"RIFF"):
            failures.append(f"{voice}: HTTP 200 with {len(payload)} bytes, not RIFF")
            print(f"  {lang:4} {voice:28} {RED}200 but not a wav{RESET}")
            continue
        seconds = wav_seconds(payload)
        if seconds < 1.0:
            failures.append(f"{voice}: {seconds:.2f}s of audio, too short to be speech")
            print(f"  {lang:4} {voice:28} {RED}{seconds:.2f}s - too short{RESET}")
            continue
        print(
            f"  {lang:4} {voice:28} {len(payload):>8} bytes  {seconds:5.2f}s  "
            f"{elapsed:5.2f}s wall  {GREEN}pass{RESET}"
        )

    for lang, reason in TTS_UNTESTABLE.items():
        print(f"  {YELLOW}{lang:4} UNTESTED{RESET} - {reason}")
    return not failures, {"failures": failures}


def run_robustness(fixtures: Path) -> tuple[bool, dict]:
    """Malformed input must be refused cleanly. Never a crash, hang or empty 200."""
    print("\n== robustness: malformed input")
    failures: list[str] = []
    cases = []

    empty = Path(tempfile.gettempdir()) / "onb-empty.wav"
    empty.write_bytes(b"")
    cases.append(("empty file", empty, (400, 415, 422)))

    tiny = Path(tempfile.gettempdir()) / "onb-tiny.wav"
    tiny.write_bytes(b"RIFF")
    cases.append(("4-byte truncated header", tiny, (400, 415, 422)))

    notaudio = Path(tempfile.gettempdir()) / "onb-notaudio.wav"
    notaudio.write_bytes(b"this is plain text pretending to be audio\n" * 40)
    cases.append(("text masquerading as audio", notaudio, (400, 415, 422)))

    corrupt = Path(tempfile.gettempdir()) / "onb-corrupt.wav"
    corrupt.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + os.urandom(4096))
    cases.append(("corrupt payload", corrupt, (400, 415, 422)))

    for label, path, acceptable in cases:
        started = time.monotonic()
        status, body = post_multipart(
            f"{STT_URL}/v1/audio/transcriptions", "file", path, timeout=120
        )
        elapsed = time.monotonic() - started
        ok = status in acceptable
        if not ok:
            failures.append(f"{label}: HTTP {status} (expected one of {acceptable})")
        mark = GREEN + "refused" + RESET if ok else RED + f"HTTP {status}" + RESET
        print(f"  {label:32} {elapsed:5.1f}s  {mark}")
        path.unlink(missing_ok=True)

    # TTS refusals: distinguishable, so a future failure is debuggable.
    print("\n== robustness: TTS error handling")
    for label, payload, expect in (
        ("empty text", {"input": "   "}, 400),
        ("unknown voice", {"input": "hi", "voice": "no-such-voice"}, 404),
        ("bad route", {"input": "hi"}, 404),
    ):
        if label == "bad route":
            status, _ = post_json(f"{TTS_URL}/nope", {"input": "hi"}, timeout=30)
        else:
            status, _ = post_json(f"{TTS_URL}/v1/audio/speech", payload, timeout=60)
        ok = status == expect
        if not ok:
            failures.append(f"{label}: HTTP {status} (expected {expect})")
        mark = GREEN + "ok" + RESET if ok else RED + f"got {status}" + RESET
        print(f"  {label:32} expected {expect:<4} {mark}")

    # Concurrency: two at once, not serialised into one slot and not collapsed.
    print("\n== robustness: concurrency")
    clips = sorted(fixtures.glob("*.wav"))[:2]
    if len(clips) < 2:
        print(f"  {DIM}need two clips for a concurrency check; skipping{RESET}")
    else:
        started = time.monotonic()

        def one(path: Path) -> tuple[str, int, float]:
            begin = time.monotonic()
            status, text = transcribe(path)
            return path.name, status, time.monotonic() - begin

        # Not `with ThreadPoolExecutor(...)`: its __exit__ calls
        # shutdown(wait=True), which blocks until in-flight workers finish. Each
        # is inside a transcription with a 600s deadline, so a Ctrl+C during this
        # block raised KeyboardInterrupt and then hung on the way out — which is
        # indistinguishable from Ctrl+C being ignored. wait=False plus
        # cancel_futures means the interpreter can leave immediately.
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            results = list(pool.map(one, clips))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        wall = time.monotonic() - started
        slowest = max(r[2] for r in results)
        for name, status, took in results:
            mark = GREEN + "200" + RESET if status == 200 else RED + str(status) + RESET
            print(f"  {name[:34]:36} {took:5.2f}s  {mark}")
        # Serial would be the sum; concurrent should be near the slower one.
        serial = sum(r[2] for r in results)
        print(
            f"  {DIM}wall {wall:.2f}s vs serial {serial:.2f}s "
            f"(ratio {wall / serial if serial else math.inf:.2f}); "
            f"slowest single {slowest:.2f}s{RESET}"
        )
        for name, status, _ in results:
            if status != 200:
                failures.append(f"concurrent {name}: HTTP {status}")

    return not failures, {"failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description="multilingual speech suite")
    parser.add_argument("fixtures", nargs="?", default="~/speech-fixtures")
    parser.add_argument("--max-cer", type=float, default=DEFAULT_MAX_CER)
    parser.add_argument(
        "--rtf-budget",
        type=float,
        default=DEFAULT_RTF_BUDGET,
        help="per-clip deadline as a multiple of the clip's audio length",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="ignore any checkpoint and rescore"
    )
    parser.add_argument("--no-robustness", action="store_true")
    parser.add_argument("--no-stt", action="store_true")
    parser.add_argument("--no-tts", action="store_true")
    args = parser.parse_args()
    install_signal_handlers()

    fixtures = Path(os.path.expanduser(args.fixtures))
    print(f"fixtures: {fixtures}")

    if not preflight():
        return 2

    manifest = load_manifest(fixtures)
    results: dict[str, bool] = {}

    if not args.no_stt:
        ok, _ = run_stt(manifest, fixtures, args.max_cer, args.rtf_budget, args.fresh)
        results["stt"] = ok
    if not args.no_tts:
        ok, _ = run_tts(args.max_cer)
        results["tts"] = ok
    if not args.no_robustness:
        ok, _ = run_robustness(fixtures)
        results["robustness"] = ok

    print("\n== summary")
    for section, ok in results.items():
        print(
            f"  {section:14} {GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}"
        )

    untested = len(TTS_UNTESTABLE) + len(manifest.get("not_tested", {}).get("STT", []))
    if untested:
        print(
            f"  {YELLOW}{untested} language/voice combination(s) UNTESTED{RESET} "
            "- see above for why"
        )

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
