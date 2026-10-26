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
#   3. the NLTK data English G2P needs - byte-verified, and laid out the
#      way nltk.data.find looks for it: the zip kept beside its extracted
#      directory, under the category directory. g2p_en checks for the zip
#      file itself, so extracting the archives anywhere else makes them
#      invisible and the first English synthesis quietly reaches for the
#      network instead - which offline-first must not do
#   4. the tsukuyomi model itself - 39,652,717 bytes from Hugging Face,
#      byte-verified, never resumed from a partial
#   5. its config, shipped in this repository: piper-plus's PhonemeType
#      enum accepts only openjtalk/bilingual/multilingual, and the model
#      directory's own json says "text", which piper-plus refuses outright
#      with ValueError (reproduced on the PC with the identical version).
#      The PC server patches text -> multilingual at load time
#      (piper-ja/server_ja.py); the phone ships the patched content, so
#      there is no load-time patch to forget
#   6. a real synthesis in Japanese and English, so "installed" means
#      both phonemizers produced audio, not files landed
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
MODEL_PATH="$MODELS/${MODEL}.onnx"
# Two paths, two names, on purpose. REPO_CONFIG is the proven json the
# repository ships; CONFIG is where it lands beside the model, which is
# where piper-plus looks for it by naming convention (model + ".json").
# The first install attempt gave one variable two jobs: it checked the
# destination existed before copying into it - a test that can only fail
# - while the message claimed the repository copy was missing. Source and
# destination are never the same file here, so neither can be confused
# for the other again.
REPO_CONFIG="$APP/scripts/voice-configs/${MODEL}.onnx.json"
CONFIG="$MODELS/${MODEL}.onnx.json"

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
# Fetched with byte verification instead of NLTK's own downloader, which
# fetches without verifying - first synthesis is the worst moment to
# discover a truncated zip on a phone.
#
# The layout is copied from what NLTK's downloader leaves behind on the
# PC, because two different consumers read it: g2p_en does
# nltk.data.find('taggers/averaged_perceptron_tagger.zip') - the zip
# file itself, not the directory - and PerceptronTagger loads the
# extracted directory beside it. An archive extracted anywhere else is
# invisible to both, and the runtime papers over it by downloading at
# first synthesis, which fails offline. The first install extracted into
# the nltk root instead of the category directory, so this layout is
# re-checked on every run rather than assumed.
#
# nltk_get category zip want_bytes - done when the extracted directory
# exists AND the zip beside it is byte-exact; either half alone is a
# half-install.
nltk_get() {
  _cat="$1" _zip="$2" _want="$3"
  _name="${_zip%.zip}"
  _dir="$NLTK_DIR/$_cat/$_name"
  _path="$NLTK_DIR/$_cat/$_zip"
  if [ -d "$_dir" ] && [ "$(file_bytes "$_path")" = "$_want" ]; then
    printf '  %-38s present\n' "$_cat/$_zip"
    return 0
  fi
  mkdir -p "$NLTK_DIR/$_cat"
  fetch_exact \
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/$_cat/$_zip" \
    "$_path" "$_want" "$_zip" || return 1
  # Zip roots differ by category: corpora/* and taggers/* archives are
  # rooted at the resource name, with no category prefix - so a prefix
  # is stripped when present and the archive is extracted into the
  # category directory either way. Either root lands at
  # <nltk>/<category>/<resource>/..., which is what nltk.data.find
  # resolves.
  "$VENV/bin/python" - "$NLTK_DIR" "$_cat" "$_path" <<'PY' || return 1
import os
import shutil
import sys
import zipfile

dest, cat, zpath = sys.argv[1], sys.argv[2], sys.argv[3]
prefix = cat + "/"
with zipfile.ZipFile(zpath) as archive:
    for member in archive.infolist():
        name = member.filename
        if name.startswith(prefix):
            name = name[len(prefix):]
        if not name:
            continue
        if name.startswith("/") or ".." in name.split("/"):
            raise SystemExit(f"refusing unsafe member: {member.filename!r}")
        target = os.path.join(dest, cat, name)
        if member.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)
print(f"  extracted under {dest}/{cat}/")
PY
}

nltk_get corpora cmudict.zip 896069 || die "cmudict fetch failed"
nltk_get taggers averaged_perceptron_tagger.zip 2526731 ||
  die "tagger fetch failed"
# The english tagger: nltk >= 3.9 loads taggers/averaged_perceptron_tagger_eng/
# for pos_tag, and without it pos_tag raises or downloads at first use.
nltk_get taggers averaged_perceptron_tagger_eng.zip 1539115 ||
  die "eng tagger fetch failed"

# --- 4. model + config -----------------------------------------------------
# Checked before the download: a repository that predates the voice
# should fail in a second, not after 38 MB.
[ -f "$REPO_CONFIG" ] || die "config missing in repo: scripts/voice-configs/${MODEL}.onnx.json"
# And validated against the installed runtime before the download, too:
# piper-plus is the only authority on which values its config parser
# accepts, and the first device install proved why that matters - it
# refused phoneme_type "text" with a ValueError the old ordering
# surfaced only after the model had fetched. PiperConfig.from_dict is
# piper's own parser, so a mismatch fails here in a second, with the
# runtime's real complaint instead of a smoke-test mystery.
# The `|| die` must share the line with `<<'PY'`: a `||` at the end of
# the heredoc's opening line makes the shell swallow the NEXT physical
# line as heredoc body, so python reads `die ...` as its stdin
# (IndentationError) and the handler never arms - the first device run
# of this check failed exactly that way, gated by nothing.
NLTK_DATA="$NLTK_DIR" "$VENV/bin/python" - "$REPO_CONFIG" <<'PY' || die "config rejected by piper-plus"
import json
import sys

from piper.config import PiperConfig

with open(sys.argv[1], encoding="utf-8") as handle:
    raw = json.load(handle)
cfg = PiperConfig.from_dict(raw)
print(
    f"config:   accepted by piper-plus "
    f"(phoneme_type={cfg.phoneme_type.value}, {cfg.num_languages} languages)"
)
PY
mkdir -p "$MODELS"
fetch_exact "$MODEL_URL" "$MODEL_PATH" "$MODEL_BYTES" "${MODEL}.onnx" ||
  die "model download failed"
# Copied from the repository on every run, so a repo update reaches the
# model dir without a re-download. The config is ours, not upstream's.
cp "$REPO_CONFIG" "$CONFIG" || die "config copy failed"
printf 'config:   copied to %s (%s bytes)\n' "$CONFIG" "$(file_bytes "$CONFIG")"

# --- 5. smoke synthesis ----------------------------------------------------
# "Installed" means audio, not files: load the model exactly as the
# server will and render through both phonemizers - Japanese through
# pyopenjtalk (the job this script exists for) and English through
# g2p_en, which is what puts step 3's NLTK layout to the test without a
# network. Same call path as the server's /v1/audio/speech, so a pass
# here means the guest runtime is proven before the operator ever curls
# it.
printf 'smoke:    synthesizing (first load reads the model, ~10-30s on the phone)...\n'
MODEL_PATH="$MODEL_PATH" CONFIG_PATH="$CONFIG" \
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
loaded = time.monotonic() - started


def synth(text: str, language_id: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize(text, wav_file, language_id=language_id)
    data = buffer.getvalue()
    if len(data) <= 1000:
        print(
            f"  smoke:  only {len(data)} bytes of WAV for {text!r} - not real audio",
            file=sys.stderr,
        )
        sys.exit(1)
    return data


ja = synth("こんにちは、テストです。", 0)
en = synth("This is an English robustness test.", 1)
print(
    f"  smoke:  loaded in {loaded:.1f}s; ja {len(ja)} + en {len(en)} WAV bytes "
    "(intra-threads=2)"
)
PY

printf '\nguest TTS installed. Start it with: onb-stack up  (service tts-ja, port 8092)\n'
printf 'Verify from Termux:\n'
printf '  curl -s http://127.0.0.1:8091/health\n'
printf '  curl -s http://127.0.0.1:8092/health\n'
