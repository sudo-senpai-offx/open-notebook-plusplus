#!/data/data/com.termux/files/usr/bin/sh
# Vehicle 2 model switch — run in plain Termux (llama-server lives there).
#
#   sh ~/bin/onb-model ~/gguf/qwen2.5-1.5b-instruct-q4_k_m.gguf
#
# Stops any running llama-server, starts the given model on the same port
# and flags, and waits for /health. Leaves the proot stack (SurrealDB,
# API, worker, Next) untouched — only the model changes.
#
# The flags match ~/bin/onb so latency measured by llama-bench and quality
# measured through the server describe the same configuration:
#   -t 2 threads, -c 4096 ctx, -np 1 slot.
# The llama-server DEFAULT is 4 slots x 32768 = 131072 tokens of KV cache,
# ~1.6 GB for a small model on a phone. Never inherit it.

set -u

MODEL="${1:-}"
[ -n "$MODEL" ] || { echo "usage: $0 <model.gguf>"; exit 1; }
[ -f "$MODEL" ] || { echo "no such model: $MODEL"; exit 1; }

LLAMA="$HOME/llama.cpp/build/bin/llama-server"
LOG="$HOME/llama-server.log"

pid_of() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cl" in *llama-server*) echo "${p#/proc/}"; return 0 ;; esac
  done
  return 1
}

if pid=$(pid_of); then
  kill "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ] && pid_of >/dev/null 2>&1; do i=$((i + 1)); sleep 1; done
  echo "[model] stopped previous llama-server"
fi

echo "[model] starting: $(basename "$MODEL")"
nohup "$LLAMA" -m "$MODEL" -t 2 -c 4096 -np 1 --port 8080 >>"$LOG" 2>&1 &

i=0
while [ "$i" -lt 45 ]; do
  if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    echo "[model] healthy after ${i}s"
    curl -s -m 3 http://127.0.0.1:8080/v1/models \
      | tr ',' '\n' | grep '"name"' | head -1
    exit 0
  fi
  i=$((i + 1)); sleep 1
done
echo "[model] FAILED to become healthy — tail of $LOG:"
tail -12 "$LOG"
exit 1