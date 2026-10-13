#!/data/data/com.termux/files/usr/bin/sh
# Vehicle 2 embedding server — run in plain Termux (llama-server lives there).
#
#   sh ~/bin/onb-embed up
#   sh ~/bin/onb-embed down
#   sh ~/bin/onb-embed status
#
# Why a second server: llama-server answers
#   501 "This server does not support embeddings. Start it with --embeddings"
# and --embeddings makes the server embedding-ONLY, so it cannot also serve
# chat. One process cannot do both, hence a second one on its own port.
#
# Model: bge-small-en-v1.5, Q8_0, 35 MB, 384 dims, 33M params. Chosen over
# all-MiniLM-L6-v2 (same size, weaker retrieval) and far over the 0.6B
# Qwen3-Embedding, which would cost more RAM than the entire rest of the
# stack. From ggml-org, so it is built by the llama.cpp org and known to
# load. Embeddings are computed once at ingest and then only compared, so a
# small model costs almost nothing at query time - this is the one place
# where model size barely matters and retrieval breadth does.
#
# Memory: -c 512 is deliberate. Source chunks are CHUNK_SIZE=400 chars
# (~100 tokens), so 512 never truncates a chunk while keeping the session
# trivially small. The 4x32768 KV default is not inherited for the same
# reason as in ~/bin/onb.

set -u

MODEL="${EMBED_MODEL:-$HOME/gguf/bge-small-en-v1.5-q8_0.gguf}"
PORT="${EMBED_PORT:-8081}"
LLAMA="$HOME/llama.cpp/build/bin/llama-server"
LOG="$HOME/llama-embed.log"

pid_of() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    # --port 8081 is what distinguishes this from the chat server on 8080
    case "$cl" in *llama-server*--port\ 8081*) echo "${p#/proc/}"; return 0 ;; esac
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

# The chat server also matches *llama-server*, so a naive kill would take
# down chat too. Match on the port instead.
kill_embed() {
  pid=$(pid_of) || return 0
  kill "$pid" 2>/dev/null
  i=0
  while [ "$i" -lt 15 ] && pid_of >/dev/null 2>&1; do i=$((i + 1)); sleep 1; done
  echo "[embed] stopped (pid $pid)"
}

case "${1:-up}" in
  down)
    kill_embed
    ;;
  status)
    if pid=$(pid_of); then
      echo "[embed] up (pid $pid) port ${PORT}"
    else
      echo "[embed] DOWN"
    fi
    ;;
  up)
    if pid=$(pid_of); then
      echo "[embed] already up (pid $pid) port ${PORT}"
      exit 0
    fi
    if [ ! -f "$MODEL" ]; then
      echo "[embed] FATAL: model missing at $MODEL"
      echo "        fetch it with:"
      echo "        curl -L -C - --retry 5 --retry-all-errors \\"
      echo "          -o ~/gguf/bge-small-en-v1.5-q8_0.gguf \\"
      echo "          https://huggingface.co/ggml-org/bge-small-en-v1.5-Q8_0-GGUF/resolve/main/bge-small-en-v1.5-q8_0.gguf"
      exit 1
    fi
    echo "[embed] starting $(basename "$MODEL") on :${PORT} (2 threads, 512 ctx, embeddings)"
    nohup "$LLAMA" -m "$MODEL" --embeddings -t 2 -c 512 \
      --port "$PORT" >>"$LOG" 2>&1 &
    if wait_healthy; then
      echo "[embed] healthy"
      curl -s -m 3 "http://127.0.0.1:${PORT}/v1/models" | tr ',' '\n' | grep '"name"' | head -1
      echo "[embed] log: $LOG"
    else
      echo "[embed] FAILED to become healthy — tail of $LOG:"
      tail -15 "$LOG"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 up|down|status"
    exit 1
    ;;
esac