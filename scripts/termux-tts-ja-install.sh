#!/data/data/com.termux/files/usr/bin/sh
# Install the piper-plus guest TTS backend (:8092). Run in PLAIN Termux:
#
#   onb-tts-ja-install          (after install-bin.sh has run)
#
# Why this exists
# ---------------
# The tsukuyomi voice needs piper-plus, piper-plus needs onnxruntime, and
# onnxruntime's manylinux aarch64 wheel needs glibc - there is no
# Termux/bionic wheel at all. Stock piper (which does run in Termux) cannot
# run the voice either: it fails with "'espeak' is not a valid
# PhonemeType", reproduced on the PC with the identical binary. So the one
# runtime that can serve this voice lives in the proot Ubuntu guest, on
# loopback :8092, and the Termux :8091 server forwards to it. This script
# installs everything that runtime needs, in the guest:
#
#   1. a private venv (the app's venv stays untouched - ADR-014's
#      separation of concerns, and pip installs are not the app's problem)
#   2. piper-plus==1.13.0, the version proven against this model on the PC
#   3. the NLTK data English G2P needs (byte-verified, no runtime download
#      at first synthesis)
#   4. the tsukuyomi model itself - 39,652,717 bytes from Hugging Face,
#      byte-verified, never resumed from a partial
#   5. its config, shipped in this repository: the upstream config.json
#      differs in phoneme_type and was never the copy proven here
#   6. a real synthesis, so "installed" means audio came out, not files
#      landed
#
# Idempotent: every step checks before acting. Re-run freely.
set -u

MODEL=tsukuyomi-chan-6lang-fp16
MODEL_URL="https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/${MODEL}.onnx"
MODEL_BYTES=39652717
PIPER_VERSION=1.13.0

die() {
  printf 'FAILED: %s\n' "$1" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Termux side: delegate into the guest and do nothing else here. The guest
# shares this network namespace, but pip and onnxruntime must run against
# glibc, which only the guest's python has.
# ---------------------------------------------------------------------------
if [ "${1:-}" != "--guest" ]; then
  if [ ! -d /data/data/com.termux ]; then
    die "run this in Termux, or with --guest inside the Ubuntu guest"
  fi
  command -v proot-distro >/dev/null 2>&1 || die "proot-distro is not installed"
  echo "Installing the guest TTS backend inside the Ubuntu proot guest ..."
  exec proot-distro login ubuntu -- \
    sh /root/app/open-notebook-plusplus/scripts/termux-tts-ja-install.sh --guest
fi

# ---------------------------------------------------------------------------
# Guest side from here on.
# ---------------------------------------------------------------------------
APP=/root/app/open-notebook-plusplus
VENV="$APP/.venv-tts"
MODELS=/root/piper_models
NLTK_DIR=/root/nltk_data
CONFIG="$MODELS/${MODEL}.onnx.json"
MODEL_PATH="$MODELS/${MODEL}.onnx"

[ -d "$APP" ] || die "repository not found at $APP (is the stack installed?)"

# Size of a file, or 0 when it does not exist.
#
# Copied from termux-fetch-models.sh, which documents why the naive
# `wc -c < "$f" 2>/dev/null` one-liner is wrong on the device: POSIX
# applies redirections left to right, so the shell's own error for a
# missing file leaks before 2>/dev/null takes effect, and the common
# `|| echo 0` guard never fires because `tr` always succeeds. Keep the
# two in step if either changes.
file_bytes() {
  [ -f "$1" ] || { echo 0; return 0; }
  _b=$({ wc -c < "$1"; } 2>/dev/null | tr -d ' ')
  echo "${_b:-0}"
}

# fetch_exact url dest want_bytes label - copied from
# termux-fetch-models.sh: download to .part, promote only on an exact
# byte match, never resume, five attempts.
fetch_exact() {
  _u="$1"; _d="$2"; _want="$3"; _lbl="$4"
  _have=$(file_bytes "$_d")
  if [ "$_have" = "$_want" ]; then
    printf '  %-32s %10s bytes  complete\n' "$_lbl" "$_have"
    return 0
  fi
  if [ "$_have" != "0" ]; then
    printf '  %-32s %10s bytes  WRONG SIZE, discarding\n' "$_lbl" "$_have"
    rm -f "$_d"
  fi
  printf '  %-32s %10s of %10s  downloading\n' "$_lbl" "$_have" "$_want"
  _n=0
  while [ "$_n" -lt 5 ]; do
    _n=$((_n + 1))
    rm -f "$_d.part"
    curl -L --retry 3 --retry-all-errors --speed-time 60 --speed-limit 1024 \
      -o "$_d.part" "$_u" >/dev/null 2>&1 || true
    _got=$(file_bytes "$_d.part")
    if [ "$_got" = "$_want" ]; then
      mv "$_d.part" "$_d"
      printf '  %-32s %10s bytes  complete\n' "$_lbl" "$_got"
      return 0
    fi
    printf '  %-32s attempt %s gave %s bytes, retrying\n' "$_lbl" "$_n" "$_got"
  done
  rm -f "$_d.part"
  printf '  %-32s could not obtain exactly %s bytes\n' "$_lbl" "$_want"
  return 1
}

# --- 0. disk ---------------------------------------------------------------
# The venv (onnxruntime + pyopenjtalk's bundled dictionary) runs near
# 450 MB, the model adds 38, NLTK 3. Check first with headroom rather
# than failing halfway through a pip transaction on a phone's storage.
free_kb=$(df -P /root 2>/dev/null | awk 'NR==2 {print $4}')
case "$free_kb" in
  '' | *[!0-9]*) free_kb=0 ;;
esac
[ "$free_kb" -ge 700000 ] ||
  die "need ~700 MB free under /root, have $((free_kb / 1024)) MB"
printf 'disk:     %s MB free under /root\n' "$((free_kb / 1024))"

# --- 1. venv ---------------------------------------------------------------
# Seeded from the app's interpreter so the guest has exactly one python
# version in play, but private: `onb-update` and the app never see it.
if [ ! -x "$VENV/bin/python" ]; then
  [ -x "$APP/.venv/bin/python" ] || die "app venv missing at $APP/.venv"
  printf 'venv:     creating %s\n' "$VENV"
  "$APP/.venv/bin/python" -m venv "$VENV" || die "venv creation failed"
else
  printf 'venv:     present\n'
fi

# Debian-family pythons ship the venv module without ensurepip; a
# python-build-standalone (uv) interpreter has it. Ladder: ensurepip,
# then get-pip, each only when the previous left pip absent.
if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  printf 'pip:      bootstrapping\n'
  "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 ||
    {
      curl -sSL --max-time 120 https://bootstrap.pypa.io/get-pip.py \
        -o /root/get-pip.py && "$VENV/bin/python" /root/get-pip.py --quiet
    } || die "pip is unavailable in the new venv"
fi
printf 'pip:      %s\n' "$("$VENV/bin/python" -m pip --version 2>/dev/null | head -1)"

# --- 2. piper-plus ---------------------------------------------------------
have_pv=$("$VENV/bin/python" -c \
  "import importlib.metadata as m; print(m.version('piper-plus'))" 2>/dev/null || echo none)
if [ "$have_pv" = "$PIPER_VERSION" ]; then
  printf 'piper:    %s already installed\n' "$have_pv"
else
  printf 'piper:    installing piper-plus==%s (onnxruntime, pyopenjtalk, nltk)\n' \
    "$PIPER_VERSION"
  # --no-cache-dir: a wheel cache on a phone is hundreds of MB of the
  # same bytes twice. The retry ladder matters on mobile links.
  "$VENV/bin/python" -m pip install --no-cache-dir \
    --default-timeout 60 --retries 15 \
    "piper-plus==$PIPER_VERSION" || die "pip install of piper-plus failed"
fi

# --- 3. NLTK data ----------------------------------------------------------
# English G2P reads cmudict and the tagger at first English synthesis.
# Fetched here with byte verification instead: NLTK's own downloader
# fetches without verifying, and first-synthesis is the worst moment to
# discover a truncated zip on a phone.
mkdir -p "$NLTK_DIR"
nltk_item() {
  # nltk_item sub zip want_bytes - the gh-pages package layout
  _nltk_fetch="https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/$1/$2"
  fetch_exact "$_nltk_fetch" "/root/$2" "$3" "$2"
  _rc=$?
  if [ "$_rc" -eq 0 ]; then
    "$VENV/bin/python" - "$NLTK_DIR" "/root/$2" <<'PY' || return 1
import sys, zipfile
zipfile.ZipFile(sys.argv[2]).extractall(sys.argv[1])
PY
    rm -f "/root/$2"
  fi
  return "$_rc"
}
if [ -d "$NLTK_DIR/corpora/cmudict" ] &&
  [ -d "$NLTK_DIR/taggers/averaged_perceptron_tagger" ]; then
  printf 'nltk:     cmudict + tagger already extracted\n'
else
  nltk_item corpora cmudict.zip 896069 || die "cmudict fetch failed"
  nltk_item taggers averaged_perceptron_tagger.zip 2526731 || die "tagger fetch failed"
fi

# --- 4. model + config -----------------------------------------------------
mkdir -p "$MODELS"
fetch_exact "$MODEL_URL" "$MODEL_PATH" "$MODEL_BYTES" "${MODEL}.onnx" ||
  die "model download failed"
# The config is ours, not upstream's - copied from the repository on
# every run so a repo update reaches the model dir without a re-download.
[ -f "$CONFIG" ] || die "config missing in repo: scripts/voice-configs/${MODEL}.onnx.json"
cp "$CONFIG" "$MODELS/" || die "config copy failed"
printf 'config:   %s bytes (phoneme_type=%s, the proven copy)\n' \
  "$(file_bytes "$CONFIG")" \
  "$("$VENV/bin/python" -c "import json,sys;print(json.load(open(sys.argv[1]))['phoneme_type'])" "$CONFIG")"

# --- 5. smoke synthesis ----------------------------------------------------
# "Installed" means audio, not files: load the model exactly as the
# server will and render Japanese. This is the same call path the
# server's /v1/audio/speech takes, so a pass here means the guest
# runtime is proven before the operator ever curls it.
printf 'smoke:    synthesizing (first load reads the model, ~10-30s on the phone)...\n'
MODEL_PATH="$MODEL_PATH" CONFIG_PATH="$MODELS/${MODEL}.onnx.json" \
  NLTK_DATA="$NLTK_DIR" PIPER_INTRA_THREADS=2 \
  "$VENV/bin/python" - <<'PY' || die "smoke synthesis failed"
import io
import os
import sys
import time
import wave

from piper.voice import PiperVoice

started = time.monotonic()
voice = PiperVoice.load(os.environ["MODEL_PATH"], os.environ["CONFIG_PATH"])
buffer = io.BytesIO()
with wave.open(buffer, "wb") as wav_file:
    voice.synthesize("こんにちは、テストです。", wav_file, language_id=0)
data = buffer.getvalue()
took = time.monotonic() - started
if len(data) <= 1000:
    print(f"  smoke:  only {len(data)} bytes of WAV - not real audio", file=sys.stderr)
    sys.exit(1)
print(f"  smoke:  {len(data)} WAV bytes in {took:.1f}s (intra-threads=2)")
PY

printf '\nguest TTS installed. Start it with: onb-stack up  (service tts-ja, port 8092)\n'
printf 'Verify from Termux:\n'
printf '  curl -s http://127.0.0.1:8091/health\n'
printf '  curl -s http://127.0.0.1:8092/health\n'
