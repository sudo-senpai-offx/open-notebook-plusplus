#!/data/data/com.termux/files/usr/bin/sh
# Fetch the multilingual STT models and TTS voices the test suite needs.
# Run in PLAIN Termux:
#
#   sh ~/bin/onb-fetch-models            # everything missing
#   sh ~/bin/onb-fetch-models --check    # report, download nothing
#
# Why a separate script
# ---------------------
# The first piper voice we fetched was silently truncated: curl reported
# `100 60.27M` - a finished transfer - and left 14 MB on disk, three times,
# because the connection is cut on a phone link and curl's progress accounting is
# optimistic. `[ -s file ]` is not a completeness test, so the partial survived to
# the next run and piper then failed with "Model config doesn't exist", which
# names the config rather than the model.
#
# So every download here is verified against a byte count taken from the Hugging
# Face API, lands in a `.part` side file, and is promoted to its real name only
# on an exact match. A partial can never become the base of a resume, because
# resuming a corrupt prefix is what pushed that file *past* its true size.
#
# Why these sizes
# ---------------
# Whisper multilingual tiers, from ggerganov/whisper.cpp. The `.en` variants are
# English-only, which is why the installed ggml-base.en-q5_1.bin cannot do this
# work at all:
#
#   ggml-base.en-q5_1.bin            59,707,625   (installed; English only)
#   ggml-small-q5_1.bin             190,085,487   conservative tier
#   ggml-large-v3-turbo-q5_0.bin    574,041,195   the "large" tier that fits 3 GB
#
# ggml-large-v3.bin proper is 2,952 MB, which exceeds the entire RAM of the 3 GB
# floor device on its own - which is why the tier here is turbo, not large.
#
# Piper voices, from rhasspy/piper-voices. zh/ja are added for multilingual TTS;
# there is no Taiwanese, Tamil or Malay voice in any of piper's 177 voices, so
# those languages are covered by STT only and the report says so rather than
# implying coverage that does not exist.

set -eu

MODEL_DIR="$HOME/gguf"
VOICE_DIR="$HOME/piper_models"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

WHISPER_BASE="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"

# name:bytes, one per line. Kept as data so adding a tier is a one-line change
# and so the same table is what the tests assert against.
MODELS="
ggml-large-v3-turbo-q5_0.bin:574041195
ggml-small-q5_1.bin:190085487
ggml-base-q5_1.bin:59707625
"

VOICES="
en_US-amy-medium:en/en_US/amy/medium
zh_CN-huayan-medium:zh/zh_CN/huayan/medium
ja_JP-hi_fi_captain-medium:ja/ja_JP/hi_fi_captain/medium
"

# voice:onnx_bytes:json_bytes
VOICE_SIZES="
en_US-amy-medium:63201294:4882
zh_CN-huayan-medium:63201294:4822
ja_JP-hi_fi_captain-medium:76753841:5301
"

step() { printf '\n==> %s\n' "$1"; }
fail() {
  printf '\n!!! FAILED at: %s\n' "$1"
  printf '!!! Paste the last lines above. A wrong byte count here means the\n'
  printf '!!! upstream file changed - re-check the Hugging Face tree API.\n'
  exit 1
}

# fetch_exact url dest want_bytes label
# Downloads to dest.part and promotes only on an exact byte match. Never resumes.
fetch_exact() {
  _u="$1"; _d="$2"; _want="$3"; _lbl="$4"
  _have=$(wc -c < "$_d" 2>/dev/null | tr -d ' ' || echo 0)

  if [ "$_have" = "$_want" ]; then
    printf '  %-32s %10s bytes  complete\n' "$_lbl" "$_have"
    return 0
  fi
  if [ "$_have" != "0" ]; then
    printf '  %-32s %10s bytes  WRONG SIZE, discarding\n' "$_lbl" "$_have"
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    printf '  %-32s %10s of %10s  MISSING\n' "$_lbl" "$_have" "$_want"
    return 1
  fi

  printf '  %-32s %10s of %10s  downloading\n' "$_lbl" "$_have" "$_want"
  _n=0
  while [ "$_n" -lt 5 ]; do
    _n=$((_n + 1))
    rm -f "$_d.part"
    curl -L --retry 3 --retry-all-errors --speed-time 60 --speed-limit 1024 \
      -o "$_d.part" "$_u" >/dev/null 2>&1 || true
    _got=$(wc -c < "$_d.part" 2>/dev/null | tr -d ' ' || echo 0)
    if [ "$_got" = "$_want" ]; then
      mv "$_d.part" "$_d"
      printf '  %-32s %10s bytes  complete\n' "$_lbl" "$_got"
      return 0
    fi
    printf '  %-32s attempt %s gave %s bytes, retrying\n' "$_lbl" "$_n" "$_got"
  done
  printf '  %-32s could not obtain exactly %s bytes\n' "$_lbl" "$_want"
  return 1
}

mkdir -p "$MODEL_DIR" "$VOICE_DIR"

step "free space"
_avail=$(df -P "$HOME" | awk 'NR==2 {print $4}')
printf '  %s KB available on %s\n' "$_avail" "$HOME"

# Completeness is decided by re-reading the files, not by a variable set inside a
# loop. `echo | while` runs the loop in a subshell, so an `_rc=1` set there dies
# with it - the script would report success while files were still wrong-sized.
# The files are the truth; read them back and compare.
check_models() {
  _bad=0
  for name in $(echo "$MODELS" | cut -d: -f1); do
    want=$(echo "$MODELS" | grep "^$name:" | cut -d: -f2)
    have=$(wc -c < "$MODEL_DIR/$name" 2>/dev/null | tr -d ' ' || echo 0)
    [ "$have" = "$want" ] || _bad=$((_bad + 1))
  done
  echo "$_bad"
}

check_voices() {
  _bad=0
  for entry in $VOICE_SIZES; do
    name=$(echo "$entry" | cut -d: -f1)
    want_onnx=$(echo "$entry" | cut -d: -f2)
    want_json=$(echo "$entry" | cut -d: -f3)
    have_onnx=$(wc -c < "$VOICE_DIR/$name.onnx" 2>/dev/null | tr -d ' ' || echo 0)
    have_json=$(wc -c < "$VOICE_DIR/$name.onnx.json" 2>/dev/null | tr -d ' ' || echo 0)
    [ "$have_onnx" = "$want_onnx" ] || _bad=$((_bad + 1))
    [ "$have_json" = "$want_json" ] || _bad=$((_bad + 1))
  done
  echo "$_bad"
}

step "STT models -> $MODEL_DIR"
echo "$MODELS" | while IFS=: read -r name want; do
  [ -z "$name" ] && continue
  fetch_exact "$WHISPER_BASE/$name" "$MODEL_DIR/$name" "$want" "$name"
done

step "TTS voices -> $VOICE_DIR"
for entry in $VOICE_SIZES; do
  name=$(echo "$entry" | cut -d: -f1)
  onnx_bytes=$(echo "$entry" | cut -d: -f2)
  json_bytes=$(echo "$entry" | cut -d: -f3)
  path=$(echo "$VOICES" | grep "^$name:" | cut -d: -f2)
  fetch_exact "$PIPER_BASE/$path/$name.onnx" "$VOICE_DIR/$name.onnx" \
    "$onnx_bytes" "$name.onnx"
  fetch_exact "$PIPER_BASE/$path/$name.onnx.json" "$VOICE_DIR/$name.onnx.json" \
    "$json_bytes" "$name.onnx.json"
done

step "summary"
printf '  models:  %s\n' "$(find "$MODEL_DIR" -maxdepth 1 -name '*.bin' | wc -l | tr -d ' ') present"
printf '  voices:  %s\n' "$(find "$VOICE_DIR" -maxdepth 1 -name '*.onnx' | wc -l | tr -d ' ') present"

_bad_models=$(check_models)
_bad_voices=$(check_voices)

if [ "$_bad_models" -ne 0 ] || [ "$_bad_voices" -ne 0 ]; then
  printf '  NOT READY: %s model file(s) and %s voice file(s) are not at their\n' \
    "$_bad_models" "$_bad_voices"
  printf '  exact expected size.\n'
  if [ "$CHECK_ONLY" -eq 0 ]; then
    fail "one or more downloads are not at their exact expected size"
  fi
fi

if [ "$CHECK_ONLY" -eq 0 ]; then
  printf '\n==> DONE\n'
  printf 'Next:  sh ~/bin/onb-stt down && sh ~/bin/onb-stt up\n'
  printf '       (the STT model is chosen from free RAM, so restart it)\n'
else
  printf '\n==> check complete (nothing downloaded)\n'
fi