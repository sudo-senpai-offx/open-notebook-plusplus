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

MODEL="${STT_MODEL:-$HOME/gguf/ggml-base.en-q5_1.bin}"
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

pid_of() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
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
  pid=$(pid_of) || return 0
  kill "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ] && pid_of >/dev/null 2>&1; do i=$((i + 1)); sleep 1; done
  if pid=$(pid_of); then
    echo "[stt] ignoring SIGTERM; SIGKILL to $pid"
    kill -9 "$pid" 2>/dev/null
    j=0
    while [ "$j" -lt 10 ] && pid_of >/dev/null 2>&1; do j=$((j + 1)); sleep 1; done
  fi
  echo "[stt] stopped (pid $pid)"
}

# The model the running server is actually serving, from the command line.
# whisper-server loads one model at startup and never changes it, so the cmdline
# is the source of truth — no /v1/models endpoint exists on whisper-server.
served_model() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
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
      echo "[stt] already up (pid $pid) port ${PORT} model $(served_model)"
      exit 0
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
