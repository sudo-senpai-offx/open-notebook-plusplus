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

# CPU topology and the derived thread budget. Sourced, not run, so the chat
# server, the embedder and the supervisor cannot each derive their own.
#
# The budget is a fraction of total cores, not a fixed number, so an 8-core
# Cortex-A53 floor and a 4x A73 + 4x A53 phone get the same *share* and the
# phone cashes the difference in as faster threads rather than more of them.
# Android, the WebView and the guest's four services keep their half.
if [ -f "$HOME/bin/onb-cpu" ]; then
  . "$HOME/bin/onb-cpu" >/dev/null 2>&1 || true
fi
if command -v cpu_threads >/dev/null 2>&1; then
  CHAT_THREADS=$(cpu_threads chat)
  CHAT_BATCH=$(cpu_batch_threads)
  CPU_AFFINITY=$(cpu_affinity_prefix)
else
  # The library is missing or failed to source. These are the values it
  # derives on an 8-core device, so a stack with no topology library behaves
  # like the floor rather than like something undefined.
  CHAT_THREADS=2
  CHAT_BATCH=4
  CPU_AFFINITY=""
fi

# Where the ubuntu container lives, for identifying its proot processes.
#
# Derived rather than hardcoded so it follows a non-default Termux prefix, and
# matching on `/containers/ubuntu/rootfs` as well means the sweep still finds
# the sessions if this path is ever wrong - over-matching is harmless here,
# under-matching leaves an orphan holding the database.
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
ROOTFS_UBUNTU="$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs"
# Always pin these. llama-server defaults allocate 4x32768 = 131072 tokens of
# KV cache, which is ~1.6 GB for a 390 MB model and more for the 1.5B. On
# SD680 the 0.5B sits at 492 MB RSS with these values.
# `-t` is decode threads and `-tb` is prompt-ingestion threads, and they are
# different jobs: decode waits on the memory bus, prefill is compute bound and
# scales. So prefill gets the whole inference share while the decode budget
# stays reserved for the interactive path that needs it.
LLAMA_ARGS="-t $CHAT_THREADS -tb $CHAT_BATCH -c $LLAMA_CTX -np 1 --port 8080"

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
  # The guest stack, torn down by the script that starts it.
  #
  # This used to be `proot-distro kill ubuntu`, guarded by `proot-distro ps |
  # grep -q ubuntu`. proot-distro 5.3.0 has **neither command** - its list is
  # install/list/login/run/remove/rename/reset/backup/restore/clear-cache/
  # copy/sync/build/push. So `proot-distro ps` printed "unknown command" to
  # stderr (discarded), stdout was empty, grep failed, and the kill was never
  # attempted once. The guard could not report failure because it never ran.
  #
  # The guest services are visible in Termux's own /proc, and `termux-stack.sh
  # down` is what knows their names and their order. So it is asked to do the
  # teardown rather than having its list copied here - a second list is a
  # second thing to drift, and this repo has already paid for that twice.
  if command -v proot-distro >/dev/null; then
    proot-distro login ubuntu -- \
      sh /root/app/open-notebook-plusplus/scripts/termux-stack.sh down \
      >/dev/null 2>&1 || true
  fi
  # Then the proot sessions themselves, orphans included.
  #
  # A launcher killed with SIGKILL orphans its proot: `--kill-on-exit` never
  # fires, and the guest keeps running. Observed on the device as two proot
  # processes for one container after a manual recovery - 19129 orphaned, 5754
  # fresh - with the new session reporting `already running` four times against
  # the old session's services.
  #
  # The rootfs path is what identifies them. There is no `proot-distro ps` to
  # ask, so the command line is the only available answer.
  kill_proot_sessions
  # Finally, the launcher itself - which SIGTERM cannot reach.
  #
  # `onb stop` runs in its own shell, so it can signal the launcher that is
  # holding the stack up. But that launcher spends its life blocked on a
  # foreground `proot-distro login`, and a POSIX shell defers traps until the
  # foreground command returns. The guest does not return while the guest is
  # running. So the SIGTERM lands, sets a pending flag, and the `trap ...
  # exit 0` never fires - observed on the device as `kill 19015` leaving the
  # process alive, sleeping, still holding everything.
  #
  # There is no signal that resolves this except SIGKILL, and by this point
  # the services have already been torn down deliberately, so there is nothing
  # left for the trap to have cleaned up.
  kill_stragglers
}

# Any `~/bin/onb` launcher other than this one.
launcher_pids() {
  for p in /proc/[0-9]*; do
    _cl=$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null) || continue
    case "$_cl" in
      *"$HOME/bin/onb "*) echo "${p#/proc/}" ;;
    esac
  done
}

# proot processes serving the ubuntu container, orphans included.
#
# Identified by `--rootfs=`, which is the only handle available: proot-distro
# 5.3.0 offers no way to list sessions. SIGTERM first so proot can honour
# `--kill-on-exit` and take the guest with it; SIGKILL only if it will not go,
# and then the guest services have already been torn down by name, so nothing
# is left holding the database.
proot_pids() {
  for p in /proc/[0-9]*; do
    _cl=$(tr '\0' ' ' <"$p/cmdline" 2>/dev/null) || continue
    case "$_cl" in
      *"--rootfs=$ROOTFS_UBUNTU"*|*"/containers/ubuntu/rootfs"*)
        echo "${p#/proc/}" ;;
    esac
  done
}

kill_proot_sessions() {
  pids=$(proot_pids | grep -v "^$$\$" || true)
  [ -n "$pids" ] || return 0
  echo "[onb] stopping proot session(s) (pid $(echo "$pids" | tr '\n' ' '))"
  for p in $pids; do kill "$p" 2>/dev/null; done
  i=0
  while [ "$i" -lt 8 ]; do
    left=$(proot_pids | grep -v "^$$\$" || true)
    [ -n "$left" ] || return 0
    sleep 1
    i=$((i + 1))
  done
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  sleep 1
  left=$(proot_pids | grep -v "^$$\$" || true)
  if [ -n "$left" ]; then
    echo "[onb] WARNING: proot survived SIGKILL (pid $(echo "$left" | tr '\n' ' '))"
    echo "         reboot, or: kill -9 $(echo "$left" | tr '\n' ' ')"
    return 1
  fi
  return 0
}

kill_stragglers() {
  pids=$(launcher_pids | grep -v "^$$\$" || true)
  [ -n "$pids" ] || return 0
  echo "[onb] stopping launcher(s) (pid $(echo "$pids" | tr '\n' ' '))"
  # SIGTERM first in case one of them is not actually blocked, then SIGKILL,
  # because for the blocked ones only the second works.
  for p in $pids; do kill "$p" 2>/dev/null; done
  i=0
  while [ "$i" -lt 5 ]; do
    left=$(launcher_pids | grep -v "^$$\$" || true)
    [ -n "$left" ] || return 0
    sleep 1
    i=$((i + 1))
  done
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  sleep 1
  left=$(launcher_pids | grep -v "^$$\$" || true)
  if [ -n "$left" ]; then
    echo "[onb] WARNING: launcher(s) survived SIGKILL (pid $(echo "$left" | tr '\n' ' '))"
    echo "         reboot, or: kill -9 $(echo "$left" | tr '\n' ' ')"
    return 1
  fi
  echo "[onb] stopped launcher(s)"
  return 0
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

  # $CPU_AFFINITY is intentionally unquoted: it is either empty or the three
  # words `taskset -c <list>`, and quoting it would pass one argument.
  # shellcheck disable=SC2086
  nohup $CPU_AFFINITY "$LLAMA" -m "$MODEL" $LLAMA_ARGS >>"$LOG" 2>&1 &
  if [ -n "$CPU_AFFINITY" ]; then
    echo "[onb] llama-server starting (${CHAT_THREADS} threads / ${CHAT_BATCH} batch, pinned to ${CPU_FAST_MASK}, ${LLAMA_CTX} ctx)"
  else
    echo "[onb] llama-server starting (${CHAT_THREADS} threads / ${CHAT_BATCH} batch, ${LLAMA_CTX} ctx)"
  fi
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

# Machine-readable residency answer, one line of key=value.
#
# The residency supervisor needs to know whether chat is loaded, which pid is
# serving it, and which model that pid is actually serving. Every one of those
# three facts is only knowable by this script's own /proc matching and
# /v1/models parse - `pids_on_port` already discriminates :8080 from the
# embedder on :8081, and that discrimination is the bug this session spent the
# most time on. Re-deriving it in the supervisor would be a second
# implementation that can disagree with this one.
#
# So the supervisor asks, rather than looks. `resident=` is 1 only when a pid
# exists *and* /health answers, for the same reason `start_llama` requires
# both: a process that exists is not a server serving, and a stale one must
# never be reported as a loaded model.
# Resident kilobytes for a pid, or 0.
#
# VmRSS is the only local answer to "are the weights actually in RAM". The
# alternative - asking the server - cannot work: a fully swapped-out
# llama-server answers /health and /v1/models perfectly, because answering
# those touches no weights. Measured on the device with the stack reporting
# healthy: VmRSS 1748 kB against a 1066 MB model. The first generation then
# took 6.9s and VmRSS went to 972016 kB, which is the page-in.
#
# sed rather than awk, matching the orphan check below: /proc/<pid>/status is
# rewritten constantly and a read can land mid-line.
vm_rss_kb() {
  [ -n "${1:-}" ] || { printf '0'; return 0; }
  sed -n 's/^VmRSS:[[:space:]]*\([0-9]*\).*/\1/p' "/proc/$1/status" 2>/dev/null \
    | head -1 | tr -d ' '
}

probe_chat() {
  _pids=$(pids_on_port 8080 2>/dev/null || true)
  _pid=$(printf '%s\n' "$_pids" | head -1)
  _rss=$(vm_rss_kb "$_pid")
  # `resident` now means the weights are in RAM, not that a server exists.
  #
  # It previously meant "a pid exists and /health answers", and that reported a
  # model with 1.7 MB resident as loaded - so the panel, the dispatcher's load
  # decisions and the idle reaper were all working from a flag that was false
  # exactly when it mattered. A threshold is needed because RSS is never
  # exactly the weight size: mmap, the allocator's arena and lazily-faulted
  # pages all move it. 50% is well clear of a swapped-out process (which reads
  # under 1% here) and well under a warm one (972 MB against 1066 MB is 91%).
  if [ -n "$_pids" ] && llama_healthy && [ "${_rss:-0}" -ge 500000 ]; then
    _model=$(curl -s -m 5 http://127.0.0.1:8080/v1/models 2>/dev/null \
      | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
      | head -1)
    printf 'role=chat resident=1 pid=%s model=%s rss_kb=%s\n' \
      "$_pid" "${_model##*/}" "$_rss"
  else
    # `serving` is reported separately from `resident` on purpose. A process
    # answering /health with its weights in zram is neither up nor loaded, and
    # collapsing that into one bit is what made the original flag useless.
    if [ -n "$_pids" ] && llama_healthy; then
      printf 'role=chat resident=0 pid=%s model=- rss_kb=%s serving=1\n' "$_pid" "$_rss"
    else
      printf 'role=chat resident=0 pid=- model=- rss_kb=0 serving=0\n'
    fi
  fi
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
  # Our own process tree, not just $$.
  #
  # `$(already_running)` runs inside a subshell whose cmdline is identical to
  # ours and whose pid differs, so a guard that only excludes `$$` matches the
  # subshell it is running in and refuses every start. Reproduced on the
  # device as three refusals naming three different pids, which reads as three
  # stale launchers rather than one self-match.
  #
  # A genuine second launcher has the same cmdline as us too, so cmdline
  # equality cannot tell the two apart. What separates them is the process
  # tree: our own subshells hang off $$, a second launcher does not.
  #
  # Excludes both directions - ancestors (a wrapper that exec'd into us) and
  # descendants (our subshells) - because either one is us.
  _is_ours() {
    [ "$1" = "$$" ] && return 0
    _q=$1
    while [ -n "$_q" ] && [ "$_q" -gt 1 ] 2>/dev/null; do
      [ "$_q" = "$$" ] && return 0
      _q=$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$_q/status" 2>/dev/null)
    done
    # Not under us. Now check whether we are under *it* - a wrapper that
    # exec'd into this script leaves its own cmdline naming bin/onb.
    _q=$$
    while [ -n "$_q" ] && [ "$_q" -gt 1 ] 2>/dev/null; do
      [ "$_q" = "$1" ] && return 0
      _q=$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$_q/status" 2>/dev/null)
    done
    return 1
  }

  for p in /proc/[0-9]*; do
    _pid=${p#/proc/}
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *"$HOME/bin/onb"*) ;;
      *) continue ;;
    esac
    _is_ours "$_pid" && continue
    echo "$_pid"
    return 0
  done
  return 1
}

case "${1:-up}" in
  # Residency verbs. The supervisor drives chat's lifecycle directly instead of
  # going through `up`, because `up` also owns the proot session, the guest
  # stack and the terminal - none of which a load-on-demand decision may touch.
  # Each verb delegates to the function above it, so "start the chat server"
  # still has exactly one implementation and the two paths cannot drift.
  chat-up)
    start_llama || exit 1
    wait_for_llama || exit 1
    ;;
  chat-down)
    stop_llama
    ;;
  chat-status)
    probe_chat
    ;;
  # Which model would `chat-up` load, and how big it is on disk.
  #
  # The candidate search above is the only place that knows the phone's model
  # inventory, so the supervisor asks rather than re-globbing `$HOME/gguf`. It
  # needs the size *before* loading: a load that cannot fit is refused, never
  # attempted and then OOM-killed halfway (ADR-014).
  model-path)
    if [ -f "$MODEL" ]; then
      printf 'model=%s bytes=%s\n' "$MODEL" "$(wc -c <"$MODEL" | tr -d ' ')"
    else
      printf 'model=- bytes=0\n'
    fi
    ;;
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
    # Whether this launch is in the foreground, decided HERE.
    #
    # Inside the guest session stdin is a pipe, so `test -t 0` there is always
    # false and the orphan check would never arm. The value is exported and
    # read back by the guest block below.
    ONB_FOREGROUND=0
    [ -t 0 ] && ONB_FOREGROUND=1
    export ONB_FOREGROUND

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
      # busybox, so a long sleep is used - in short steps, because this loop
      # also watches for the launcher being orphaned.
      #
      # Observed: PPID=1 on the launcher while it still held the stack. Its
      # parent terminal had gone, so it was reparented to init and kept
      # running - holding :8080, :8081, the proot session and the guest stack
      # open indefinitely. `proot-distro kill ubuntu` could not reach the
      # session, because the `proot-distro login` that registered it had long
      # since exited, so `~/bin/onb stop` from a new terminal left everything
      # in place.
      #
      # A foreground process whose terminal closes should go away. Exit when
      # the parent does, and let --kill-on-exit and the teardown run: an
      # orphaned launcher holding two ports and a whole guest stack is worse
      # than no stack at all, because the next launch reports `already up`
      # against processes nothing is supervising.
      # The condition has to *persist*. A single PPID=1 sample is not proof
      # that the terminal closed: the launcher can briefly read 1 while the
      # shell that spawned it is being replaced, and tearing the stack down on
      # that looks like the stack spontaneously dying. Observed on the
      # device - `~/bin/onb` reported a duplicate within a minute of starting
      # and was gone seconds later, with nothing left running and nothing in
      # any log.
      # Only a *foreground* launcher can have its terminal close under it.
      #
      # `onb-update --restart` starts the stack with `nohup ... &`, so a
      # detached launcher has PPID=1 from the moment it begins - by design -
      # and applying the check to it tears the stack down a minute later. That
      # broke the documented restart path: the updater reported success, and
      # the stack it started was gone before anyone looked at it.
      #
      # `test -t 0` is taken here, in the launcher shell, before the
      # guest session replaces stdin with a pipe.
      _foreground="${ONB_FOREGROUND:-0}"
      _orphan=0
      while :; do
        sleep 5
        # sed rather than awk: /proc/$$/status is rewritten constantly and a
        # read can land on a partial line, which awk tolerates and sed does
        # not - but the fallback below covers that case anyway.
        _ppid=$(sed -n 's/^PPid:[[:space:]]*//p' "/proc/$$/status" 2>/dev/null)
        if [ "$_foreground" = "0" ]; then
          # Detached by the updater. Nothing to observe; just stay up.
          continue
        fi
        if [ "${_ppid:-1}" = "1" ]; then
          _orphan=$((_orphan + 1))
          # 12 samples at 5s: a full minute of sustained orphaning before the
          # stack is released.
          if [ "$_orphan" -ge 12 ]; then
            echo
            echo "[onb] parent shell closed; shutting down"
            break
          fi
        else
          _orphan=0
        fi
      done
    '
    status=$?
    echo "[onb] guest session ended (status $status)"
    stop_llama
    # The embedding server runs in Termux like the chat server, so
    # --kill-on-exit does not reap it either.
    sh "$HOME/bin/onb-embed" down >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: $0 [up|stop|chat-up|chat-down|chat-status|model-path]"
    exit 1
    ;;
esac