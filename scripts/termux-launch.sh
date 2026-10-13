#!/data/data/com.termux/files/usr/bin/sh
# Vehicle 2 launcher — run in a plain Termux session (NOT inside proot).
#
#   sh ~/bin/onb          # start everything, Ctrl+C stops everything
#   sh ~/bin/onb stop     # stop from another session
#
# Why this shape: proot is started with --kill-on-exit, so when the proot
# process exits, every guest process it spawned is reaped. That is the
# graceful-stop mechanism. The launcher therefore parks on a blocking
# `read` after bring-up; Ctrl+C interrupts it, the guest command returns,
# proot exits, and SurrealDB/API/worker/Next die together. No PID
# bookkeeping needed on the stop path.
#
# llama-server runs in TERMUX, not in the guest, so it is not covered by
# --kill-on-exit and is killed explicitly on the way out.

set -u

# Default model per docs/7-DEVELOPMENT/wayfinder-plusplus/research-cpu-infer.md
# Appendix F: the 1.5B-Q4_K_M scored 0.84 mean coverage against the 0.5B's
# 0.58 on the 25-prompt fixture, for a mean 15.5s vs 6.1s. The latency is
# affordable - warm chat turns are 2.3-4.6s - so the bigger model is the
# right default. The 0.5B is the FLOOR model for 2GB/SD439 devices, which
# cannot hold 1.04 GiB of weights alongside the stack.
#
# Falls back to the 0.5B when the 1.5B is not downloaded, so a fresh device
# starts rather than dying on a missing path. Override with
# ONB_MODEL=/path/to/model.gguf.
MODEL="${ONB_MODEL:-}"
if [ -z "$MODEL" ]; then
  for _candidate in \
    "$HOME/gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf" \
    "$HOME/gguf/qwen2.5-0.5b-instruct-q2_k.gguf"; do
    if [ -f "$_candidate" ]; then
      MODEL="$_candidate"
      break
    fi
  done
fi

LLAMA="$HOME/llama.cpp/build/bin/llama-server"
LOG="$HOME/llama-server.log"
# Always pin these. llama-server defaults allocate 4x32768 = 131072 tokens of
# KV cache, which is ~1.6 GB for a 390 MB model and more for the 1.5B. On
# SD680 the 0.5B sits at 492 MB RSS with these values.
LLAMA_ARGS="-t 2 -c 4096 -np 1 --port 8080"

pid_of() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cl" in
      *"$1"*) echo "${p#/proc/}"; return 0 ;;
    esac
  done
  return 1
}

stop_llama() {
  if pid=$(pid_of "llama-server"); then
    kill "$pid" 2>/dev/null && echo "[onb] stopped llama-server (pid $pid)"
  fi
}

stop_all() {
  stop_llama
  # Any live login session on the container; --kill-on-exit takes the rest.
  if command -v proot-distro >/dev/null && proot-distro ps 2>/dev/null | grep -q ubuntu; then
    proot-distro kill ubuntu 2>/dev/null && echo "[onb] stopped proot session(s)"
  fi
}

start_llama() {
  if pid_of "llama-server" >/dev/null; then
    echo "[onb] llama-server already running"
    return 0
  fi
  [ -f "$MODEL" ] || {
    echo "[onb] FATAL: no usable model found."
    echo "        looked for qwen2.5-1.5b-instruct-q4_k_m.gguf then"
    echo "        qwen2.5-0.5b-instruct-q2_k.gguf under \$HOME/gguf/"
    echo "        set ONB_MODEL=/path/to/model.gguf to pick one explicitly"
    return 1
  }
  [ -x "$LLAMA" ] || { echo "[onb] FATAL: llama-server missing at $LLAMA"; return 1; }

  # shellcheck disable=SC2086
  nohup "$LLAMA" -m "$MODEL" $LLAMA_ARGS >>"$LOG" 2>&1 &
  echo "[onb] llama-server starting (2 threads, 4096 ctx)"
}

wait_for_llama() {
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
      echo "[onb] llama-server healthy"
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "[onb] WARNING: llama-server not healthy after 30s (see $LOG)"
  return 1
}

case "${1:-up}" in
  stop)
    stop_all
    ;;
  up)
    trap 'stop_llama; exit 0' INT TERM
    start_llama || exit 1
    wait_for_llama

    # Bring up the guest stack, then block so --kill-on-exit has something
    # to reap when Ctrl+C arrives.
    proot-distro login ubuntu -- bash -c '
      sh /root/termux-stack.sh up
      echo
      echo "  Open Notebook: http://127.0.0.1:8502"
      echo "  Ctrl+C stops everything."
      echo
      printf "press Ctrl+C to stop: "
      read -r _
    '
    status=$?
    echo "[onb] guest session ended (status $status)"
    stop_llama
    ;;
  *)
    echo "usage: $0 [up|stop]"
    exit 1
    ;;
esac