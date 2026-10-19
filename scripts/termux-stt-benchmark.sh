#!/data/data/com.termux/files/usr/bin/sh
# Measure STT throughput on this device, so the model tier and thread count are
# chosen from evidence rather than arithmetic.
#
#   sh ~/bin/onb-stt-benchmark                    # default matrix
#   sh ~/bin/onb-stt-benchmark --clips 2          # more clips per configuration
#   sh ~/bin/onb-stt-benchmark --models small,large
#
# Why this exists
# ---------------
# The first measurement of the multilingual tier put a 4.7-second Mandarin clip
# at 228 seconds: RTF 48, on two of eight cores with a 547 MB model. Two things
# were guesses at that point - that the model was the right tier, and that two
# threads was the right number - and both had been wrong before on this device.
#
# whisper.cpp scales close to linearly with threads for the encoder, which
# dominates the cost, and the model size scales the work per token. So RTF should
# improve substantially with more threads and with a smaller model. By how much
# is exactly the thing worth measuring.
#
# What it does
# ------------
# For each (model, threads): start whisper-server with that combination, time
# one request per clip, record RTF = wall / audio_seconds. The clip's audio
# length comes from the fixture manifest, so RTF is computed against a known
# denominator rather than an estimate.
#
# The server is restarted between configurations, which is why this is a
# separate script rather than a flag on the suite: a running server cannot
# change its model or its thread count.
#
# It leaves the STT server on the last configuration it tried, and says so.

set -u

FIXTURES="${1:-$HOME/speech-fixtures}"
CLIPS=1
MODELS="small large"
THREADS="2 4 6"

while [ $# -gt 0 ]; do
  case "$1" in
    --clips) CLIPS="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    --threads) THREADS="$2"; shift 2 ;;
    --fixtures) FIXTURES="$2"; shift 2 ;;
    *) break ;;
  esac
done

MANIFEST="$FIXTURES/manifest.json"
[ -f "$MANIFEST" ] || {
  echo "no manifest at $MANIFEST"
  echo "  push the fixtures, then re-run"
  exit 2
}

MODEL_PATHS=""
for m in $MODELS; do
  case "$m" in
    small) MODEL_PATHS="$MODEL_PATHS $HOME/gguf/ggml-small-q5_1.bin" ;;
    large) MODEL_PATHS="$MODEL_PATHS $HOME/gguf/ggml-large-v3-turbo-q5_0.bin" ;;
    base)  MODEL_PATHS="$MODEL_PATHS $HOME/gguf/ggml-base-q5_1.bin" ;;
    *) echo "unknown model tier: $m"; exit 2 ;;
  esac
done

# Which clips, and how long each is, straight from the manifest.
print_clips() {
  python3 - "$MANIFEST" "$CLIPS" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1]))
n = int(sys.argv[2])
picked = []
for clip in m["clips"]:
    picked.append((clip["file"], float(clip.get("duration_s") or 0.0)))
    if len(picked) >= n:
        break
for name, dur in picked:
    print(f"{name}\t{dur}")
PYEOF
}

# One request, timed, against the STT server.
time_clip() {
  _f="$1"; _limit="$2"
  _start=$(date +%s%N 2>/dev/null || echo 0)
  _code=$(curl -s -m "$_limit" -o "$HOME/bench-out.txt" -w '%{http_code}' \
    -X POST "http://127.0.0.1:${STT_PORT:-8090}/v1/audio/transcriptions" \
    -F "file=@$FIXTURES/$_f" 2>/dev/null)
  _end=$(date +%s%N 2>/dev/null || echo 0)
  if [ "$_start" = "0" ] || [ "$_end" = "0" ] || [ "$_end" -le "$_start" ]; then
    echo "TIMEOUT"
  else
    # Nanoseconds -> seconds, with one decimal. The previous version emitted
    # tenths as "394/10" and then divided by ten again when computing RTF, so
    # every RTF was ten times too small and the WALL column was unreadable.
    # A benchmark whose headline number is wrong by a factor of ten is worse
    # than no benchmark.
    echo "scale=1; ($_end - $_start) / 1000000000" | bc 2>/dev/null \
      || echo "$(( (_end - _start) / 100000000 ))"
  fi
}

printf '\n%-10s %-8s %-34s %8s %8s %8s\n' MODEL THREADS CLIP AUDIO WALL RTF
printf -- '---------------------------------------------------------------\n'

for mp in $MODEL_PATHS; do
  [ -f "$mp" ] || { echo "$(basename "$mp"): not installed, skipping"; continue; }
  _mname=$(basename "$mp" .bin | sed 's/^ggml-//')
  for t in $THREADS; do
    echo
    echo "-- $_mname with $t thread(s)"
    sh "$HOME/bin/onb-stt" down >/dev/null 2>&1
    STT_MODEL="$mp" STT_THREADS_OVERRIDE="$t" sh "$HOME/bin/onb-stt" up >/dev/null 2>&1
    # The server needs a moment to load the model before the first request.
    i=0
    while [ "$i" -lt 30 ]; do
      curl -sf -m 2 "http://127.0.0.1:${STT_PORT:-8090}/health" >/dev/null 2>&1 && break
      i=$((i + 1)); sleep 1
    done

    print_clips | while IFS="$(printf '\t')" read -r name dur; do
      [ -n "$name" ] || continue
      wall=$(time_clip "$name" 900)
      if [ "$wall" = "TIMEOUT" ]; then
        rtf=">900"
        wtxt="TIMEOUT"
      else
        # wall is tenths; duration is seconds -> RTF = (wall/10) / dur
        rtf=$(python3 -c "print(f'{$wall/$dur:.1f}')" 2>/dev/null || echo "?")
        wtxt="$wall"
      fi
      printf '%-10s %-8s %-34s %8s %8s %8s\n' "$_mname" "$t" "$name" "$dur" "$wtxt" "$rtf"
    done
  done
done

printf -- '\n---------------------------------------------------------------\n'
printf 'RTF is wall time divided by audio length. Below 1.0 means the\n'
printf 'phone transcribes faster than the audio plays. The shipped default\n'
printf 'should be the cheapest tier that lands under 1.0 for a lecture-length\n'
printf 'clip, since transcription is a queued job nobody waits on.\n'
printf '\nThe STT server is left on the last configuration tried.\n'
