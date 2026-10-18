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

# One number, used twice. The API trims a conversation to this many
# tokens; llama-server is started with the same value as its window.
# Keeping them as two literals means one can be changed and the other
# forgotten, and the symptom is a prompt the server silently truncates.
LLAMA_CTX="${OPEN_NOTEBOOK_CHAT_CONTEXT:-4096}"
OPEN_NOTEBOOK_CHAT_CONTEXT="$LLAMA_CTX"
export OPEN_NOTEBOOK_CHAT_CONTEXT

# Once-a-day update check, before anything is started. Checks only - it
# never merges, because merging during a launch is exactly the conflict
# onb-update refuses to run into, and it would make start-up depend on
# the network. Silent on every outcome except "there is something".
# Failure here must never stop the launch.
if [ -f "$HOME/bin/onb-update" ]; then
  sh "$HOME/bin/onb-update" --launch-check 2>/dev/null || true
fi

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
LLAMA_ARGS="-t 2 -c $LLAMA_CTX -np 1 --port 8080"

pids_of() {
  # Every process whose *executable* is $1. Not the first match.
  #
  # A previous version returned one pid, and `llama-server` is not
  # reliably a single process: the launcher found pid 26053 at 3 MB,
  # signalled it, reported success, and the server still did not come
  # up - the same pid reappeared after `pkill -f llama-server` had run,
  # meaning something was still holding the port that was not the pid
  # being signalled. Killing the whole set removes the need to know
  # which one it is.
  for p in /proc/[0-9]*; do
    argv0=$(tr '\0' '\n' 2>/dev/null < "$p/cmdline" | head -1) || continue
    [ -n "$argv0" ] || continue
    case "${argv0##*/}" in
      "$1") echo "${p#/proc/}" ;;
    esac
  done
}

# True only when something is actually answering. A process existing is
# not a server serving, and start_llama consults both so a stale
# process can never suppress a real start.
llama_healthy() {
  curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1
}

  # Processes for `llama-server` listening on $1.
  #
  # The chat server (:8080) and the embedder (:8081) are the same executable,
  # so matching by name returns both. `start_llama` then decided the *embedder*
  # was stale - it had just tested :8080, which is not up yet, so nothing was
  # answering - and killed it along with the process it meant to replace. The
  # embedder deferred SIGTERM until its request finished, told the next check
  # `already up`, and exited about fifteen seconds later. Both logs showed the
  # same thing:
  #
  #     llama_server: listening on http://127.0.0.1:8081
  #     ... ~15s ...
  #     operator(): cleaning up before exit...
  #
  # Two logs, two graceful exits, both ~15s. Not a crash and not the OOM
  # killer: a scheduled signal that was never meant for them.
  pids_on_port() {
    for p in /proc/[0-9]*; do
      argv0=$(tr '\0' '\n' 2>/dev/null < "$p/cmdline" | head -1) || continue
      [ -n "$argv0" ] || continue
      [ "${argv0##*/}" = "llama-server" ] || continue
      case "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" in
        *"--port $1"*) echo "${p#/proc/}" ;;
      esac
    done
  }

# Clear every match, escalating. SIGTERM first because llama-server
# frees its KV cache on the way out and the phone has 8 GB to lose;
# SIGKILL only if it does not go.
kill_llama() {
  pids=$(pids_on_port 8080)
  [ -n "$pids" ] || return 0
  for p in $pids; do kill "$p" 2>/dev/null; done
  i=0
  while [ "$i" -lt 5 ]; do
    llama_healthy || return 0
    sleep 1
    i=$((i + 1))
  done
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  sleep 1
  return 0
}

stop_llama() {
  pids=$(pids_on_port 8080)
  [ -n "$pids" ] || return 0
  kill_llama
  echo "[onb] stopped llama-server (pid $(echo "$pids" | tr '\n' ' '))"
}

stop_all() {
  stop_llama
  # The embedder, which `stop_llama` deliberately does not touch.
  #
  # Making the chat lifecycle port-scoped was right - it stopped the chat
  # server from killing the embedder - but it left :8081 with no owner on the
  # stop path, because only the `up` teardown ever called onb-embed down.
  # Ctrl+C followed by `onb stop` therefore left the embedder running,
  # reparented to init and still holding :8081, so the next launch reported
  # `already up` against a server nothing was supervising.
  sh "$HOME/bin/onb-embed" down >/dev/null 2>&1 || true
  # Any live login session on the container; --kill-on-exit takes the rest.
  if command -v proot-distro >/dev/null && proot-distro ps 2>/dev/null | grep -q ubuntu; then
    proot-distro kill ubuntu 2>/dev/null && echo "[onb] stopped proot session(s)"
  fi
}

start_llama() {
  # A process matching is not the same as a server serving. Requiring
  # both means the two checks can no longer disagree in the direction
  # that leaves nothing listening: if a match exists but health fails,
  # clear them all out and start a real one.
  if pids=$(pids_on_port 8080) && [ -n "$pids" ]; then
    if llama_healthy; then
      echo "[onb] llama-server already running (pid $(echo "$pids" | tr '\n' ' '))"
      return 0
    fi
    echo "[onb] stale llama-server (pid $(echo "$pids" | tr '\n' ' ')) is not serving; replacing"
    kill_llama
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
  echo "[onb] llama-server starting (2 threads, ${LLAMA_CTX} ctx)"
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
  echo "[onb]   nothing is answering on :8080, so chat will fail."
  echo "[onb]   tail -5 $LOG for the reason; if the port is held by a"
  echo "[onb]   process this launcher cannot see, reboot the phone."
  return 1
}

# Refuse a second launcher while one is already up.
#
# Two `~/bin/onb` processes each start a chat server and each run onb-embed up,
# and they race: the second sees the embedder mid-start, gets no health check
# back inside its timeout, decides it is stale, and starts another. Observed on
# the device - five processes claiming :8081, two of which appeared between two
# consecutive checks seconds apart. llama-server binds with SO_REUSEPORT, so
# several can hold the port and the kernel hands requests to whichever it picks,
# some of which have no model loaded.
#
# The two launchers then fight on the way out: whichever exits first runs
# stop_llama and onb-embed down and takes the other's servers with it. Same
# failure as the EOF bug, reached by a different route.
already_running() {
  for p in /proc/[0-9]*; do
    [ "$p" = "/proc/$$" ] && continue
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *"$HOME/bin/onb"*) echo "${p#/proc/}"; return 0 ;;
    esac
  done
  return 1
}

case "${1:-up}" in
  stop)
    stop_all
    ;;
  up)
    if other=$(already_running); then
      echo "[onb] already running (pid $other) - not starting a second launcher."
      echo "      Two launchers race for :8080 and :8081, and the first to exit"
      echo "      stops the other's servers. Use the running one, or:"
      echo "        ~/bin/onb stop      # then start again"
      exit 1
    fi
    trap 'stop_llama; exit 0' INT TERM
    start_llama || exit 1
    wait_for_llama

    # Embedding server. Started here rather than left to the operator
    # because the app's default_embedding_model now points at :8081 - if
    # that server is absent after a reboot, source ingestion stops
    # embedding while still reporting success, which is the worst kind of
    # failure to debug. Best-effort: a device without the model still
    # boots, just without vector search.
    if [ -f "${EMBED_MODEL:-$HOME/gguf/bge-small-en-v1.5-q8_0.gguf}" ]; then
      sh "$HOME/bin/onb-embed" up || echo "[onb] embedding server failed - vector search off"
    else
      echo "[onb] no embedding model - skipping (vector search off)"
    fi

    # Bring up the guest stack, then block so --kill-on-exit has something
    # to reap when Ctrl+C arrives.
    proot-distro login ubuntu -- bash -c '
      # The repo copy, not /root/termux-stack.sh. A second copy at the guest
      # root was installed once at setup and nothing ever refreshed it, so
      # every fix to the launcher reached ~/bin/onb-stack and not the file
      # this line actually executes. The loopback binding was added to one
      # copy while the other kept `--host 0.0.0.0`, and the symptom -
      # reachable from the LAN - persisted through a restart, a reinstall,
      # and a clean kill, because every signal pointed at the file that was
      # not being run.
      sh /root/app/open-notebook-plusplus/scripts/termux-stack.sh up
      echo
      echo "  Open Notebook: http://127.0.0.1:8502"
      echo "  Ctrl+C stops everything."
      echo
      printf "press Ctrl+C to stop: "
      # Block on something that cannot return early.
      #
      # `read -r _` returns the instant stdin reaches EOF - a closed terminal,
      # a detached session, a shell that hands over a pipe - and the proot
      # session then ends, which runs stop_llama and onb-embed down and takes
      # both model servers with it. Observed: the stack reported `up` at
      # launch, and some minutes later uvicorn, llama-server and the embedder
      # were gone while surreal, the worker and next-server survived. The
      # llama log showed a clean `listening` followed by
      # `operator(): cleaning up before exit...` 39s later - a signal, not the
      # OOM killer, and nothing in the output said who sent it.
      #
      # A sleep loop cannot hit EOF. `sleep infinity` is not portable to
      # busybox, so the large finite value is used instead.
      while :; do sleep 2147483647; done
    '
    status=$?
    echo "[onb] guest session ended (status $status)"
    stop_llama
    # The embedding server runs in Termux like the chat server, so
    # --kill-on-exit does not reap it either.
    sh "$HOME/bin/onb-embed" down >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 [up|stop]"
    exit 1
    ;;
esac