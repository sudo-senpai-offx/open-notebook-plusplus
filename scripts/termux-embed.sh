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

  # The model the running server is actually serving, by basename.
  #
  # `/v1/embeddings` ignores the `model` field entirely and echoes it back, so
  # asking for "jina" against a server running bge-small returns bge-small's
  # 384-dim vectors labelled jina. The only honest source of truth is
  # `/v1/models`.
  served_model() {
    curl -s -m 5 "http://127.0.0.1:${PORT}/v1/models" \
      | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      | head -1 \
      | sed 's|.*/||'
  }

  # Refuse to continue when the server is serving something else.
  #
  # The mismatch is silent and total: the app embeds at the served model's
  # dimensions while the stored index holds another model's, so every search
  # returns nothing and nothing is logged anywhere. This is the same shape as
  # the rebuild-status 500 - a gap between two components that each look fine.
  #
  # The `already up` path is where this actually bites: a server left over
  # from a previous launch keeps its own model, and reporting "already up"
  # without looking is how a stale embedder survives a model change.
  require_expected_model() {
    _expected=$(basename "$MODEL")
    _actual=$(served_model)
    [ -n "$_actual" ] || return 0   # cannot tell; do not invent a failure
    if [ "$_actual" != "$_expected" ]; then
      echo "[embed] FATAL: serving '$_actual' but EMBED_MODEL expects '$_expected'" >&2
      echo "         a stale embedding server from an earlier launch is still up." >&2
      echo "         its vectors will not match the stored index, and search" >&2
      echo "         will silently return nothing. Fix with:" >&2
      echo "           sh ~/bin/onb-embed down && ~/bin/onb" >&2
      return 1
    fi
    return 0
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
      require_expected_model || exit 1
      echo "[embed] already up (pid $pid) port ${PORT} model $(served_model)"
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
      # Pull the model name out of the JSON rather than splitting on commas.
      # `tr ',' '\n'` put a whole `{"models":[{"name":"/data/...` prefix on
      # every startup line, because the first comma comes *after* the name
      # field - so the filter printed the envelope it was meant to strip.
      # jq is not assumed present on the phone.
      # Captured, not piped straight to echo. `head -1` does not add a
      # trailing newline when its input lacks one - llama-server's last
      # line has none - so the model path ran straight into the next echo:
      #
      #     .../bge-m3-Q4_K_M.gguf[embed] log: ...
      #
      # Command substitution strips any trailing newline and `echo` puts one
      # back, which also gives somewhere to report when the name cannot be
      # parsed at all - previously that printed nothing at all.
      _served=$(
        curl -s -m 3 "http://127.0.0.1:${PORT}/v1/models" \
          | sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
          | head -1
      )
      echo "[embed] model: ${_served:-not reported}"
      # Verified after a fresh start too, not only on the already-up path:
      # a port already held by something else answers /health happily and
      # /v1/models with its own model.
      require_expected_model || exit 1
      # Report the dimension it produces. This is the number that has to
      # agree with the stored `source_embedding` vectors, and printing it
      # here is the only place an operator can see the two side by side
      # before a rebuild silently fails to match. Costs one embed, which is
      # ~0.06s on bge-small and ~25s on bge-m3 - set EMBED_PROBE=0 to skip.
      if [ "${EMBED_PROBE:-1}" != "0" ]; then
        _dims=$(curl -s -m 120 -X POST "http://127.0.0.1:${PORT}/v1/embeddings" \
          -H 'Content-Type: application/json' \
          -d '{"input":"dimension probe"}' \
          | grep -o '\[-[-0-9.eE+,]*\]' \
          | sed 's/^\[//; s/\]$//' \
          | tr ',' '\n' | grep -c . )
        echo "[embed] dims:  ${_dims:-?}"
        echo "[embed]       stored source_embedding vectors must match this"
      fi
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