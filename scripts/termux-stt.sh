#!/data/data/com.termux/files/usr/bin/sh
# Vehicle 4 speech-to-text server — run in plain Termux (whisper.cpp).
#
#   sh ~/bin/onb-stt up
#   sh ~/bin/onb-stt down
#   sh ~/bin/onb-stt status
#   sh ~/bin/onb-stt probe
#   sh ~/bin/onb-stt model-path
#
# Why a separate server: whisper-server transcribes audio; it is not a language
# model and cannot serve chat or embeddings. One process, one port, one model.
#
# Model: whisper base.en q5_1, ~60 MB. Small enough to keep resident on an
# 8 GB phone alongside chat and embed, large enough for accurate lecture
# transcription. Chosen over large-v3-turbo (574 MB — would cost more RAM
# than chat + embed combined and defeat the residency design).
#
# Memory: there is no context flag to set. whisper-server takes its audio
# context from the model itself; `-mc/--max-context` is a *text* context and is
# not the same thing. An earlier version of this script passed `-c 512`, which
# does not exist. whisper-server treats an unknown argument as fatal — prints
# "error: unknown argument: -c" and exits, with status 0, so a check that only
# looked at the exit code would have reported a clean start.
#
# Audio input: the server expects 16 kHz mono WAV unless `--convert` is given,
# in which case it shells out to ffmpeg to transcode whatever was uploaded. A
# lecture arrives as m4a or mp3, so `--convert` is what makes it work at all.
# ffmpeg is checked at `up` rather than assumed, because its absence surfaces as
# an empty transcript on the first recording rather than as a startup error.
#
# Endpoints: /health (liveness), /v1/audio/transcriptions (OpenAI-compatible,
# same shape as the PC whisper server so one client serves both).
#
set -u

# Model choice is a function of free RAM, not a constant.
#
# The installed default was ggml-base.en-q5_1.bin, which is English-only - it
# cannot transcribe the zh/ja/ta/my material this app is meant to handle. The
# multilingual tiers differ by 10x in size, and the difference decides whether
# the device survives:
#
#   ggml-small-q5_1.bin             190 MB   conservative tier
#   ggml-large-v3-turbo-q5_0.bin    574 MB   the "large" tier that fits 3 GB
#   ggml-large-v3.bin              2952 MB   exceeds a 3 GB device on its own
#
# The floor is 3 GB RAM with an octa-core A53 (Snapdragon 439), so a hardcoded
# "large" is a claim the weakest supported device cannot honour. Instead the
# largest tier that fits the current budget wins, and the budget leaves headroom
# for the app itself - this is the same reasoning as the residency gate that
# refuses a model when there is no room, applied before the model is even loaded.
#
# STT_MODEL overrides everything, so a specific model can still be forced.
MODEL="${STT_MODEL:-}"

# Headroom kept for the app, the scheduler, piper and the OS. Not a model cost:
# it is what must remain available once whisper-server has the model mapped.
STT_RESERVE_MB="${STT_RESERVE_MB:-600}"

# Set by pick_model; empty when STT_MODEL overrides the choice.
STT_TIER_CHOSEN=""
STT_BUDGET_MB=""

mem_available_mb() {
  # MemAvailable, not MemFree: MemFree ignores reclaimable page cache, which on
  # Android is most of the file cache, and would report a phone with plenty of
  # room as being full.
  #
  # Echoed unconditionally, and divided down to MB. Two bugs in one line, both
  # caught by the test that runs this with a fake /proc/meminfo rather than by any
  # assertion on the text:
  #
  #   `[ -n "$_kb" ] || echo 0`  prints only on failure, so on success this
  #     returned empty, the budget became `0 - 600`, and every tier was rejected.
  #     No model would ever have been selected on the phone.
  #   echoing `_kb` raw        kB compared against MB, so the budget was ~1024x
  #     too large and the largest tier always won regardless of memory.
  #
  _kb=$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)
  echo $(( ${_kb:-0} / 1024 ))
}

# ---------------------------------------------------------------------------
# The language -> model policy
#
# Measured on this device, 10.58s of English audio per clip, four threads,
# FLEURS ground truth, the language forced so the comparison is like for like:
#
#     language   small-q5_1 (181 MB)      large-v3-turbo-q5_0 (574 MB)
#     en         CER 0.009  RTF 1.8       CER 0.000  RTF  9.3
#     ja         CER 0.153  RTF 1.4       CER 0.016  RTF  7.3
#     ta         CER 0.144  RTF 2.4       CER 0.109  RTF 13.0
#     zh         CER 0.222  RTF 3.5       CER 0.000  RTF 20.2
#     my         CER 0.836  RTF 14.6      CER 0.455  RTF 29.3
#
# Two things fall out of that.
#
# Mandarin is the one language where the *small* model is not merely less
# accurate but *wrong*: CER 0.222 there means fluent Mandarin that is not what
# was said. For a study app whose text is searched, a plausible wrong
# transcript is worse than a slow one, so Mandarin gets the big model.
#
# Malay does not work on either tier - the model does not speak it, and large
# still lands at CER 0.455 producing mixed-script noise. It is refused
# explicitly rather than transcribed into gibberish, because a clear "not
# supported" beats a transcript that is confidently wrong.
#
# Everything else takes the small model: it is five times faster and accurate
# enough, and a 10-minute lecture goes from 93 minutes to 18.

STT_LANG_LARGE="zh tw"

# Languages the models do not speak. Refused, not mistranscribed.
STT_LANG_UNSUPPORTED="my"

stt_model_for_language() {
  case "$1" in
    my|ms|malay|"")
      # Empty means "not declared", which is not the same as "Malay". The
      # caller decides; here the small model is the safe default because it is
      # the fast one and an unknown language is usually English.
      ;;
  esac
  for _l in $STT_LANG_LARGE; do
    [ "$1" = "$_l" ] && { echo "$HOME/gguf/ggml-large-v3-turbo-q5_0.bin"; return 0; }
  done
  echo "$HOME/gguf/ggml-small-q5_1.bin"
}

stt_language_supported() {
  for _l in $STT_LANG_UNSUPPORTED; do
    [ "$1" = "$_l" ] && return 1
  done
  return 0
}

pick_model() {
  _budget=$(($(mem_available_mb) - STT_RESERVE_MB))
  _picked=""; _picked_mb=0; _picked_name=""
  # Preference order, smallest-and-fastest first.
  #
  # This used to be "largest that fits in RAM", on the reasoning that a bigger
  # model is a better model. Measured on an 8-core phone, 10.58s of English
  # audio, four threads:
  #
  #     small-q5_1     181 MB   20.2s   RTF 1.9
  #     large-v3-turbo 574 MB  104.8s   RTF 9.9
  #
  # The bigger model is five times slower, and on this hardware that is the
  # difference between a lecture being searchable in a minute and in an hour.
  # Memory was never the binding constraint; time is. So the default is the
  # fastest tier that fits, and the big one is opt-in for when its accuracy is
  # wanted and the wait is acceptable - `STT_MODEL=...`.
  for tier in \
      "small:$HOME/gguf/ggml-small-q5_1.bin" \
      "base:$HOME/gguf/ggml-base-q5_1.bin" \
      "large:$HOME/gguf/ggml-large-v3-turbo-q5_0.bin" \
      "base_en:$HOME/gguf/ggml-base.en-q5_1.bin"; do
    _name=${tier%%:*}
    _path=${tier#*:}
    [ -f "$_path" ] || continue
    _mb=$(( $(wc -c < "$_path" | tr -d ' ') / 1024 / 1024 ))
    [ "$_mb" -le "$_budget" ] || continue
    # The FIRST tier that fits wins. The order above *is* the preference.
    #
    # A "keep the largest" guard was left over from when the order was
    # largest-first, and it quietly defeated the new order: the list was
    # reordered to put the fast model first, and the guard then went on to
    # pick the biggest one that fitted. The shipped default stayed exactly
    # what it had been, and nothing said so.
    _picked="$_path"; _picked_mb=$_mb; _picked_name=$_name
    break
  done
  MODEL="$_picked"
  STT_TIER_CHOSEN="$_picked_name"
  STT_BUDGET_MB="$_budget"
}

# A language may be given as a second argument: `up zh`, `model-path zh`.
# The app knows the language of the source it is transcribing, and the tier
# follows from it - the phone owns the model files, so the phone owns the
# policy.
#
# Parsed here rather than next to the case statement, because the model
# selection below reads it and a script that uses a variable before it is set
# gets an empty one - which silently means "the small model" and no error.
STT_LANG="${2:-}"

if [ -n "$STT_LANG" ] && ! stt_language_supported "$STT_LANG"; then
  case "${1:-up}" in
    up|model-path)
      echo "[stt] language '$STT_LANG' is not supported" >&2
      echo "        the multilingual models do not speak it: large-v3-turbo" >&2
      echo "        still scores CER 0.455 on Malay, producing mixed-script" >&2
      echo "        noise rather than a transcript." >&2
      exit 3
      ;;
  esac
fi

if [ -n "$STT_LANG" ]; then
  MODEL=$(stt_model_for_language "$STT_LANG")
  STT_TIER_CHOSEN="lang:$STT_LANG"
elif [ -z "$MODEL" ]; then
  pick_model
  if [ -z "$MODEL" ]; then
    echo "[stt] FATAL: no STT model fits. Available $(mem_available_mb) MB," \
      "budget $(( $(mem_available_mb) - STT_RESERVE_MB )) MB after a ${STT_RESERVE_MB}MB reserve."
    echo "        Fetch models with:  sh ~/bin/onb-fetch-models"
    echo "        Or lower the reserve:  STT_RESERVE_MB=300 sh ~/bin/onb-stt up"
    exit 1
  fi
fi

PORT="${STT_PORT:-8090}"
WHISPER="$HOME/whisper.cpp/build/bin/whisper-server"
LOG="$HOME/whisper-server.log"

# The same derived budget the chat server uses, from the same library — chat,
# embed, and STT share one inference allowance so they cannot collectively
# starve the device.
if [ -f "$HOME/bin/onb-cpu" ]; then
  . "$HOME/bin/onb-cpu" >/dev/null 2>&1 || true
fi
if command -v cpu_threads >/dev/null 2>&1; then
  STT_THREADS=$(cpu_threads stt)
  CPU_AFFINITY=$(cpu_affinity_prefix)
else
  STT_THREADS=2
  CPU_AFFINITY=""
fi

  # An explicit thread count overrides the derived budget.
  #
  # The derived budget gives STT two threads on an eight-core phone, which is
  # right when a transcription competes with a podcast render and wrong when it
  # is the only inference running - and transcription is a *queued* job, so
  # nobody is waiting on it. The first measurement of the multilingual tier put
  # a 4.7-second clip at 228 seconds, RTF 48, on two of eight cores.
  #
  # So the budget is a default, not a constraint. An override also makes the
  # tier and thread count measurable rather than assumed: the benchmark varies
  # them and reports RTF, and the winner becomes the shipped default.
  if [ -n "${STT_THREADS_OVERRIDE:-}" ]; then
    STT_THREADS="$STT_THREADS_OVERRIDE"
  fi
pid_of() {
  for p in /proc/[0-9]*; do
    cl=$({ tr '\0' ' ' < "$p/cmdline"; } 2>/dev/null) || continue
    # --port 8090 distinguishes STT from chat (8080), embed (8081), TTS (8091)
    case "$cl" in *whisper-server*--port\ 8090*) echo "${p#/proc/}"; return 0 ;; esac
  done
  return 1
}

wait_healthy() {
  i=0
  while [ "$i" -lt 60 ]; do
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1)); sleep 1
  done
  return 1
}

kill_stt() {
  # `|| return 0` rather than falling through to a "stopped" message: if
  # pid_of finds nothing there is nothing to stop, and saying "stopped" would
  # report an action that was never taken.
  pid=$(pid_of) || { echo "[stt] not running"; return 0; }
  kill "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ] && pid_of >/dev/null 2>&1; do i=$((i + 1)); sleep 1; done
  if _still=$(pid_of); then
    echo "[stt] ignoring SIGTERM; SIGKILL to $_still"
    kill -9 "$_still" 2>/dev/null
    j=0
    while [ "$j" -lt 10 ] && pid_of >/dev/null 2>&1; do j=$((j + 1)); sleep 1; done
  fi
  # The pid that was actually signalled, not whatever `pid` holds by now. An
  # earlier version reused `pid` for the SIGKILL check and then printed it, so
  # a server that was never running reported `stopped (pid )` — claiming an
  # action it did not take, against an empty pid.
  echo "[stt] stopped (pid $pid)"
}

# The model the running server is actually serving, from the command line.
# whisper-server loads one model at startup and never changes it, so the cmdline
# is the source of truth — no /v1/models endpoint exists on whisper-server.
served_model() {
  for p in /proc/[0-9]*; do
    cl=$({ tr '\0' ' ' < "$p/cmdline"; } 2>/dev/null) || continue
    case "$cl" in *whisper-server*--port\ 8090*)
      # The value after the model flag. `up` starts the server with `-m`, and
      # grepping for `--model` found nothing — so the probe reported an empty
      # model for a server that was demonstrably serving one. The supervisor's
      # key/value parser needs a non-empty value to match at all, so the field
      # vanished rather than showing as blank, and the status panel had nothing
      # to say about which model was loaded.
      echo "$cl" | tr ' ' '\n' \
        | grep -A1 -e '^-m$' -e '^--model$' \
        | grep -v -e '^-m$' -e '^--model$' -e '^--$' \
        | head -1 | sed 's|.*/||'
      return 0
    esac
  done
  echo ""
}

# Machine-readable residency answer, matching probe_chat/probe_embed shape.
probe_stt() {
  # The tier and the budget are reported, not implied. The model is chosen from
  # free RAM at start, so "which model" and "why that one" are the first two
  # questions when a transcript comes back wrong on a device that previously
  # worked - and neither is answerable from the process alone once it is running.
  # The model reported is the one *served*, not the one selected. Those differ
  # whenever the server was started with an explicit STT_MODEL and is still
  # running while the launcher's own selection has moved on - which is exactly
  # what a tier comparison does. Reporting the selection here made the probe
  # say `small` while `large` was serving, and the suite keys its checkpoint on
  # this line, so a re-measurement under one model could silently reuse
  # another model's results.
  if [ -n "$MODEL" ] && [ -f "$MODEL" ]; then
    _serving=$(served_model)
    printf '[stt] model=%s tier=%s budget_mb=%s available_mb=%s reserve_mb=%s\n' \
      "${_serving:-$(basename "$MODEL")}" "${STT_TIER_CHOSEN:-override}" \
      "${STT_BUDGET_MB:--}" "$(mem_available_mb)" "$STT_RESERVE_MB"
  fi
  if _pid=$(pid_of) && [ -n "$_pid" ]; then
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      printf 'role=stt resident=1 pid=%s model=%s rss_kb=%s\n' \
        "$_pid" "$(served_model)" "$(vm_rss_kb "$_pid")"
    else
      printf 'role=stt resident=0 pid=%s model=- rss_kb=%s serving=1\n' "$_pid" "$(vm_rss_kb "$_pid")"
    fi
  else
    printf 'role=stt resident=0 pid=- model=- rss_kb=0 serving=0\n'
  fi
}

# Resident kilobytes for a pid, or 0. Same measurement as termux-launch.sh:
# VmRSS is the only honest answer to "are the weights in RAM".
vm_rss_kb() {
  [ -n "${1:-}" ] || { printf '0'; return 0; }
  sed -n 's/^VmRSS:[[:space:]]*\([0-9]*\).*/\1/p' "/proc/$1/status" 2>/dev/null \
    | head -1 | tr -d ' '
}

# Which model `up` would load, and how big it is on disk. See the identical
# verb in termux-launch.sh / termux-embed.sh: the supervisor needs the size
# before loading so a load that cannot fit is refused rather than attempted.
model_path() {
  if [ -n "$STT_LANG" ]; then
    _m=$(stt_model_for_language "$STT_LANG")
    if [ -f "$_m" ]; then
      printf 'model=%s bytes=%s\n' "$_m" "$(wc -c <"$_m" | tr -d ' ')"
    else
      printf 'model=- bytes=0\n'
    fi
    return 0
  fi
  if [ -f "$MODEL" ]; then
    printf 'model=%s bytes=%s\n' "$MODEL" "$(wc -c <"$MODEL" | tr -d ' ')"
  else
    printf 'model=- bytes=0\n'
  fi
}

case "${1:-up}" in
  down)
    kill_stt
    ;;
  probe)
    probe_stt
    ;;
  model-path)
    # Dispatched, not merely defined. The supervisor calls this to size the
    # model before loading it, and a script that defines `model_path` without
    # an arm for it falls through to the usage message and exits 1 - which the
    # supervisor reads as "size unknown", refuses the load, and reports every
    # role as unloaded. That reads exactly like a phone that is out of memory.
    model_path
    ;;
  status)
    if pid=$(pid_of); then
      echo "[stt] up (pid $pid) port ${PORT}"
    else
      echo "[stt] DOWN"
    fi
    ;;
  up)
    if pid=$(pid_of); then
      # Already up - but is it up with the *right* model?
      #
      # The selection changed once already: the shipped default went from the
      # 574 MB multilingual tier to the 181 MB one, because the big model is
      # five times slower on this hardware. The running server was never
      # restarted, so it kept serving the old model and the change was
      # invisible - `onb-stt up` reported "already up" and the operator had no
      # way to tell that the thing they had just installed was not the thing
      # running.
      _served=$(served_model)
      _want=$(basename "$MODEL")
      # A language may ask for a different tier than the one running.
      if [ "$_served" = "$_want" ]; then
        echo "[stt] already up (pid $pid) port ${PORT} model $_served"
        exit 0
      fi
      echo "[stt] up with a different model (running $_served, want $_want) - restarting"
      kill_stt
    fi
    if [ ! -f "$MODEL" ]; then
      echo "[stt] FATAL: model missing at $MODEL"
      echo "        fetch it with:"
      echo "        curl -L -C - --retry 5 --retry-all-errors \\"
      echo "          -o ~/gguf/ggml-base.en-q5_1.bin \\"
      echo "          https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin"
      exit 1
    fi
    [ -x "$WHISPER" ] || {
      echo "[stt] FATAL: whisper-server missing at $WHISPER"
      echo "        build it in Termux:"
      echo "          pkg install clang cmake make git ffmpeg"
      echo "          git clone https://github.com/ggerganov/whisper.cpp ~/whisper.cpp"
      echo "          cd ~/whisper.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --config Release -j\$(nproc)"
      exit 1
    }
    # ffmpeg is only needed to *decode* the upload, and its absence does not
    # stop the server starting — it fails on the first non-WAV file, with an
    # empty transcript and no clue why. Said here, where the operator is
    # already running the command that needs it.
    if ! command -v ffmpeg >/dev/null 2>&1; then
      echo "[stt] WARNING: ffmpeg not found. WAV uploads will work;"
      echo "        m4a/mp3/mp4 uploads will return an empty transcript."
      echo "        Install it with:  pkg install ffmpeg"
    fi
    echo "[stt] starting $(basename "$MODEL") on :${PORT} (${STT_THREADS} threads, whisper.cpp)"
    # `--convert` is what lets the server accept m4a/mp3/mp4 at all; without it
    # it expects 16 kHz mono WAV on the wire. No `-c`: that flag does not exist
    # and an unknown argument makes whisper-server exit.
    # shellcheck disable=SC2086
    nohup $CPU_AFFINITY "$WHISPER" -m "$MODEL" -t "$STT_THREADS" \
      --host 127.0.0.1 --port "$PORT" \
      --inference-path /v1/audio/transcriptions \
      --convert \
      >>"$LOG" 2>&1 &
    if wait_healthy; then
      echo "[stt] healthy"
    else
      echo "[stt] FAILED to become healthy — tail of $LOG:"
      tail -15 "$LOG"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 up|down|status|probe|model-path"
    exit 1
    ;;
esac
