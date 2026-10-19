#!/data/data/com.termux/files/usr/bin/sh
# Vehicle 5 text-to-speech server — run in plain Termux (piper).
#
#   sh ~/bin/onb-tts up
#   sh ~/bin/onb-tts down
#   sh ~/bin/onb-tts status
#   sh ~/bin/onb-tts probe
#   sh ~/bin/onb-tts model-path
#
# Why a separate server: piper synthesizes speech; it is not a language model
# and cannot serve chat, embeddings, or STT. One process, one port, one voice.
#
# Model: en_US-amy-medium.onnx, ~45 MB. Small enough to keep resident on an
# 8 GB phone alongside chat, embed, and STT. Piper synthesizes on demand and
# does not keep audio in RAM between requests, so the resident cost is the
# model weights only (~45 MB + Python + Flask overhead).
#
# Memory: piper loads the ONNX model once at startup. Threads from onb-cpu so
# TTS shares the same inference budget as chat, embed, and STT.
#
# Endpoints: /v1/models (voice listing), /v1/audio/speech (OpenAI-compatible
# TTS, same shape as the PC piper server).
#
# Note: piper server.py is hardcoded to port 9600. The Termux copy
# (~/piper-server.py) reads TTS_PORT from the environment so it can run on
# :8091 without colliding with the PC's :9600.
#
set -u

MODEL="${TTS_MODEL:-$HOME/piper_models/en_US-amy-medium.onnx}"
PORT="${TTS_PORT:-8091}"
TTS_SERVER="$HOME/bin/onb-tts-server"
PIPER_BIN="${PIPER_BIN:-$HOME/piper-build/prefix/bin/piper}"
LOG="$HOME/piper-server.log"

# piper's RPATH is baked at install time and points at the prefix piper was
# *installed into*, which is not where it now lives: piper installs flat
# (`DESTINATION ${CMAKE_INSTALL_PREFIX}`) and the build then moves the binary to
# bin/, so the recorded path is stale from the moment it is relocated. The
# libraries sit beside the binary instead, and the loader does not search an
# executable's own directory, so it needs LD_LIBRARY_PATH to find them.
#
# Set here rather than in the server because the server hands piper to
# `subprocess.run`, which inherits this environment — one place, and a caller
# who invokes piper by hand gets the same answer.
PIPER_LIB_DIR=$(dirname "$PIPER_BIN")
PIPER_PREFIX_DIR=$(dirname "$PIPER_LIB_DIR")
LD_LIBRARY_PATH="$PIPER_LIB_DIR:$PIPER_PREFIX_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH

# The same derived budget the chat server uses, from the same library — chat,
# embed, STT, and TTS share one inference allowance so they cannot collectively
# starve the device. TTS gets the stt allocation (same class: media I/O).
if [ -f "$HOME/bin/onb-cpu" ]; then
  . "$HOME/bin/onb-cpu" >/dev/null 2>&1 || true
fi
if command -v cpu_threads >/dev/null 2>&1; then
  TTS_THREADS=$(cpu_threads stt)
  CPU_AFFINITY=$(cpu_affinity_prefix)
else
  TTS_THREADS=2
  CPU_AFFINITY=""
fi

pid_of() {
  for p in /proc/[0-9]*; do
    # `{ ...; } 2>/dev/null` rather than `tr ... < file 2>/dev/null`: redirections
    # apply left to right, so a `<` on a pid that exited between the glob and the
    # read fails before the stderr redirect is in place, and the *shell's*
    # redirection error lands on the terminal. That is a `cannot open
    # /proc/<pid>/cmdline` on every probe that races a server restart. The braces
    # put the failed redirection inside the redirected command.
    cl=$({ tr '\0' ' ' < "$p/cmdline"; } 2>/dev/null) || continue
    # Match the server script, not a port. The port is passed in the
    # *environment* (`TTS_PORT=8091 python3 .../onb-tts-server`), so it never
    # appears on the command line at all — an earlier version matched on
    # `piper-server.py` and `8091` together and therefore never found its own
    # process, so `up` would start a second server on every invocation and
    # `down` would report nothing to stop.
    case "$cl" in *onb-tts-server*) echo "${p#/proc/}"; return 0 ;; esac
  done
  return 1
}

wait_healthy() {
  i=0
  while [ "$i" -lt 60 ]; do
    # piper does not have /health; /v1/models returns 200 when ready
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1)); sleep 1
  done
  return 1
}

kill_tts() {
  # `|| return 0` rather than falling through to a "stopped" message: if pid_of
  # finds nothing there is nothing to stop, and saying "stopped" would report an
  # action that was never taken.
  pid=$(pid_of) || { echo "[tts] not running"; return 0; }
  kill "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ] && pid_of >/dev/null 2>&1; do i=$((i + 1)); sleep 1; done
  if _still=$(pid_of); then
    echo "[tts] ignoring SIGTERM; SIGKILL to $_still"
    kill -9 "$_still" 2>/dev/null
    j=0
    while [ "$j" -lt 10 ] && pid_of >/dev/null 2>&1; do j=$((j + 1)); sleep 1; done
  fi
  # The pid that was actually signalled, not whatever `pid` holds by now. Reusing
  # `pid` for the SIGKILL check made a server that was never running report
  # `stopped (pid )` — claiming an action it did not take, against an empty pid.
  echo "[tts] stopped (pid $pid)"
}

# The voice the running server is serving, read from its environment.
served_model() {
  # The voice comes from TTS_MODEL in the *environment*, so it has to be read
  # from there. `/proc/<pid>/cmdline` shows only `python3 <script>` and the
  # supervisor's status panel would show an empty model for a server that is
  # demonstrably serving one. Same UID in Termux, so /proc/<pid>/environ is
  # readable.
  for p in /proc/[0-9]*; do
    # Braces, for the same reason as in pid_of: a pid can exit between the glob
    # and the read, and an un-braced `2>/dev/null` does not cover the shell's own
    # redirection error. This is the line that printed
    # `cannot open /proc/<pid>/cmdline: No such file` on a probe.
    cl=$({ tr '\0' ' ' < "$p/cmdline"; } 2>/dev/null) || continue
    case "$cl" in *onb-tts-server*)
      { tr '\0' '\n' < "$p/environ"; } 2>/dev/null \
        | sed -n 's/^TTS_MODEL=//p' | head -1 | sed 's|.*/||;s|\.onnx$||'
      return 0
      ;;
  esac
  done
  echo ""
}

# Machine-readable residency answer, matching probe_chat/probe_embed shape.
probe_tts() {
  if _pid=$(pid_of) && [ -n "$_pid" ]; then
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
      printf 'role=tts resident=1 pid=%s model=%s rss_kb=%s\n' \
        "$_pid" "$(served_model)" "$(vm_rss_kb "$_pid")"
    else
      printf 'role=tts resident=0 pid=%s model=- rss_kb=%s serving=1\n' "$_pid" "$(vm_rss_kb "$_pid")"
    fi
  else
    printf 'role=tts resident=0 pid=- model=- rss_kb=0 serving=0\n'
  fi
}

# Resident kilobytes for a pid, or 0.
vm_rss_kb() {
  [ -n "${1:-}" ] || { printf '0'; return 0; }
  sed -n 's/^VmRSS:[[:space:]]*\([0-9]*\).*/\1/p' "/proc/$1/status" 2>/dev/null \
    | head -1 | tr -d ' '
}

# Which model `up` would load, and how big it is on disk.
model_path() {
  if [ -f "$MODEL" ]; then
    printf 'model=%s bytes=%s\n' "$MODEL" "$(wc -c <"$MODEL" | tr -d ' ')"
  else
    printf 'model=- bytes=0\n'
  fi
}

case "${1:-up}" in
  down)
    kill_tts
    ;;
  probe)
    probe_tts
    ;;
  model-path)
    # Dispatched, not merely defined. See the identical note in termux-stt.sh:
    # an undispatched verb falls through to the usage message, and the
    # supervisor reads that exit code as "size unknown" and refuses the load.
    model_path
    ;;
  status)
    if pid=$(pid_of); then
      echo "[tts] up (pid $pid) port ${PORT}"
    else
      echo "[tts] DOWN"
    fi
    ;;
  up)
    if pid=$(pid_of); then
      echo "[tts] already up (pid $pid) port ${PORT} model $(served_model)"
      exit 0
    fi
    if [ ! -f "$MODEL" ]; then
      echo "[tts] FATAL: voice model missing at $MODEL"
      echo "        onb-build-piper fetches one; or drop any .onnx voice into"
      echo "        ~/piper_models/ and set TTS_MODEL to its name"
      exit 1
    fi
    if [ ! -f "$TTS_SERVER" ]; then
      echo "[tts] FATAL: server missing at $TTS_SERVER"
      echo "        it ships as scripts/termux-tts-server.py; install it with"
      echo "          sh install-bin.sh"
      echo "        or copy it to $TTS_SERVER by hand"
      exit 1
    fi
    if [ ! -x "$PIPER_BIN" ]; then
      echo "[tts] FATAL: piper binary missing at $PIPER_BIN"
      echo "        Termux has no piper package, so it has to be built:"
      echo "          sh ~/bin/onb-build-piper"
      exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
      echo "[tts] FATAL: python3 not found"
      echo "        pkg install python"
      exit 1
    fi
    echo "[tts] starting $(basename "$MODEL") on :${PORT} (${TTS_THREADS} threads, piper)"
    # shellcheck disable=SC2086
    TTS_PORT="$PORT" \
      TTS_MODELS_DIR="$(dirname "$MODEL")" \
      TTS_MODEL="$(basename "$MODEL" .onnx)" \
      TTS_THREADS="$TTS_THREADS" \
      PIPER_BIN="$PIPER_BIN" \
      TTS_HOST=127.0.0.1 \
      nohup $CPU_AFFINITY python3 "$TTS_SERVER" \
      >>"$LOG" 2>&1 &
    if wait_healthy; then
      echo "[tts] healthy"
    else
      echo "[tts] FAILED to become healthy — tail of $LOG:"
      tail -15 "$LOG"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 up|down|status|probe|model-path"
    exit 1
    ;;
esac
