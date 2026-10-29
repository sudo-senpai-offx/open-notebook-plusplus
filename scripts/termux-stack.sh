#!/bin/sh
# Vehicle 2 stack launcher — run INSIDE the proot Ubuntu guest, or from a
# plain Termux session as `sh ~/bin/onb-stack up|down|status` (the Termux
# side delegates; see the block above the dispatch).
#
# Why this exists: proot is started with --kill-on-exit, so every guest
# process dies when the proot session that spawned it closes. That is how
# SurrealDB "disappeared" mid-session. Nothing started here survives the
# session either — but this script makes bring-up idempotent and cheap
# enough to re-run after any restart, instead of four hand-typed commands
# with a dozen env vars between them.
#
# Usage: sh /root/termux-stack.sh up|down|status      (inside the guest)
#    or: sh ~/bin/onb-stack up|down|status            (Termux - delegates)
#
# Not a supervisor. It does not restart on crash and does not survive the
# proot session. Keeping the session alive (tmux) is the durable answer;
# this is the fast path.

set -u

APP=/root/app/open-notebook-plusplus
FRONTEND=/root/frontend
DATA=/root/snb-data
LOG=/root/stack.log
NODE="$APP/.venv/lib/python3.12/site-packages/nodejs_wheel/bin/node"
# The piper-plus TTS venv (:8092) - optional, created by onb-tts-ja-install.
TTS_VENV="$APP/.venv-tts"

# Process lookup by executable, with arguments matched separately.
# `ss`/`fuser`/`pkill` are not installed in this guest, so /proc is the
# only option. Next rewrites its own process title to "next-server
# (v16.2.6)", so the executable name is the stable part to match — never
# "server.js", and never a bare substring of the whole command line.
pids_of() {
  # An interpreter runs the program as an argument, so for anything started
  # through a venv console script argv[0] is the interpreter and not the
  # program. `.venv/bin/uvicorn` is executed by the venv python, which means
  # matching argv[0] alone reported `uvicorn DOWN` and
  # `surreal-commands-worker DOWN` for processes that were serving requests
  # at that very moment - and, worse, made `is_up` false, so every `up`
  # started a second copy.
  #
  # Shells are deliberately not in the interpreter list. A diagnostic like
  # `bash -c '... uvicorn api.main:app ...'` would otherwise match itself,
  # which is precisely the confusion this function was rewritten once to
  # avoid.
  pattern_head=${1%% *}
  pattern_tail=${1#"$pattern_head"}
  pattern_tail=${pattern_tail# }
  for p in /proc/[0-9]*; do
    argv0=$(tr '\0' '\n' 2>/dev/null < "$p/cmdline" | head -1) || continue
    [ -n "$argv0" ] || continue
    base=${argv0##*/}
    # Next rewrites its own process title to `next-server (v16.3.8)`, so
    # argv[0] arrives with the version already glued on. `${argv0##*/}` strips
    # path components, not a version suffix, so the comparison below could
    # never match and `next-server` reported DOWN for a web server that was
    # serving pages - which is also why `is_up` said no and `up` started a
    # second one. Strip the parenthetical before comparing.
    base=${base%% (*}
    matched=0
    case "$base" in
      "$pattern_head")
        tr '\0' ' ' 2>/dev/null < "$p/cmdline" | grep -q -- "$pattern_tail" && matched=1
        ;;
      python | python3 | node)
        # The whole pattern, head and tail, as it appears in the command
        # line of an interpreter-hosted process.
        tr '\0' ' ' 2>/dev/null < "$p/cmdline" | grep -q -- "$1" && matched=1
        ;;
    esac
    [ "$matched" = 1 ] || continue
    echo "${p#/proc/}"
  done
}

pid_of() {
  # Return the first matching pid, and only the first.
  #
  # pids_of prints one pid per line, so the first *line* is what has to be
  # taken. Stripping at the first space is wrong in a way that only shows up
  # when there are two matches: llama-server is reliably two processes, and
  # the status table printed
  #
  #     llama  up  pid 30758
  #     30789 ? kB
  #
  # because the whole multi-line value was substituted into `/proc/$pid/status`
  # and awk read nothing. `is_up` was unaffected - a non-empty string is still
  # non-empty - so the gate kept working while the display quietly lied.
  #
  # `pids_of | head -1` is the obvious way to take the first line and is what
  # this replaced, but head closes the pipe on the first line and every later
  # echo inside pids_of then fails with EPIPE, printed by the shell as
  # `echo: I/O error` in the middle of the status table. Capturing first and
  # splitting afterwards keeps nothing on the other end of a pipe.
  _pids=$(pids_of "$1" 2>/dev/null) || return 1
  [ -n "$_pids" ] || return 1
  # A literal newline inside the expansion word: POSIX-portable, and no
  # subshell or pipe to leak an I/O error from.
  _first=${_pids%%'
'*}
  printf '%s\n' "$_first"
}

is_up() { pid_of "$1" >/dev/null 2>&1; }

# Does the API answer on loopback? Used to confirm a start actually took,
# rather than trusting that the backgrounded process outlived its own bind
# error.
api_answers() {
  .venv/bin/python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:5055/health", timeout=3).read()
PY
}

# Wait for the API to answer, rather than assuming a fixed delay is enough.
#
# The first version slept 2 seconds and reported `FAILED to start` on a
# phone whose uvicorn was perfectly healthy and answering a second later -
# visible only because /health was reachable over the tunnel afterwards. A
# guard that cries wolf is worse than no guard: the next genuine failure
# gets read as the same noise.
#
# Startup here is slow for a known reason: the command modules are imported
# to populate the command registry, and ADR-009 measured heavy imports as
# costing around a minute on low-end hardware. So the wait is generous and
# polling, not a guess.
wait_for_api() {
  waited=0
  limit="${1:-90}"
  while [ "$waited" -lt "$limit" ]; do
    if api_answers; then return 0; fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

# Show what the API said, not what the last process to write said.
#
# Every process appends to one shared log, so `tail` on a start failure
# showed SurrealDB's startup lines - which say nothing about why uvicorn
# did not come up. Filtered to lines that could plausibly be the API's, and
# labelled as a filtered view so nobody reads it as the whole story.
report_api_failure() {
  echo "api      FAILED to start - 5055 not answering after ${1:-90}s."
  echo "         Lines mentioning the API or an error, from the shared log:"
  grep -iE 'uvicorn|5055|error|traceback|address already' "$LOG" 2>/dev/null \
    | tail -n 6 | sed 's/^/           /'
  echo "         If it says 'address already in use', a previous uvicorn"
    echo "         survived 'down'. pkill is NOT installed in this guest - the"
    echo "         note at the top of this file says so - so signal the pid"
    echo "         directly, read from the same /proc scan:"
    echo "           sh ~/bin/onb-stack status   # shows the pid"
    echo "           kill <that-pid>              # from Termux, not the guest"
}

# Report what the binding actually is - and admit when it cannot tell.
#
# It cannot usually tell. Android hides /proc/net from apps, so `netstat`
# shows nothing, and `ip -4 addr` does not list wlan0, so the LAN address is
# often unavailable. An earlier version of this check treated an empty
# address as "bound to loopback" and printed a clean bill of health for a
# service that was listening on 0.0.0.0 - a guard reporting success because
# it knew nothing, which is the failure this function exists to prevent.
#
# The authoritative check is from another host, against this device's
# address. This is a local sanity signal only, and says "cannot verify"
# rather than guessing.
report_binding() {
  want="$1"
  lan=$(ip -4 addr show wlan0 2>/dev/null | grep -oE 'inet [0-9.]+' | head -1 | cut -d' ' -f2)
  if [ -z "$lan" ]; then
    echo "api      binding: cannot verify from here (no wlan0 address visible);"
    echo "         requested $want. Confirm from another host:"
    echo "           curl http://<device-lan-ip>:5055/api/auth/status"
    return 0
  fi
  if .venv/bin/python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://$lan:5055/health', timeout=2).read()
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
    echo "api      binding: WARNING reachable at $lan:5055, not loopback-only"
    return 1
  fi
  echo "api      binding: loopback-only (refused at $lan:5055)"
}

kill_of() {
  pids=$(pids_of "$1")
  [ -n "$pids" ] || return 0
  for p in $pids; do kill "$p" 2>/dev/null; done
  echo "stopped $1 (pid $(echo "$pids" | tr '\n' ' '))"
}

# Wait for a process to actually disappear. `down` immediately followed by
# `up` used to race: SIGTERM is asynchronous, so the old uvicorn still held
# :5055 when the replacement tried to bind, and the replacement died with
# EADDRINUSE — which surfaced as "API did not come back up".
wait_gone() {
  i=0
  while [ "$i" -lt 20 ]; do
    # A zombie's cmdline is empty, so a killed process stops matching here
    # even before its parent reaps it.
    pids_of "$1" >/dev/null 2>&1 && [ -n "$(pids_of "$1")" ] || return 0
    i=$((i + 1))
    sleep 1
  done

    # Escalate to SIGKILL.
    #
    # Warning and giving up meant `down` did not reliably down anything: the
    # only signal sent was SIGTERM, and llama-server defers SIGTERM until its
    # current request finishes. With a slow embedder that is tens of seconds
    # per call, so the process outlived the shutdown, kept holding :8081, and
    # every later launch reported "already up" against a server serving the
    # wrong model. One bge-m3 process survived three restarts this way.
    _left=$(pids_of "$1" 2>/dev/null)
    if [ -n "$_left" ]; then
      echo "[stack] $1 ignored SIGTERM; SIGKILL to $(echo "$_left" | tr '\n' ' ')"
      for p in $_left; do kill -9 "$p" 2>/dev/null; done
      j=0
      while [ "$j" -lt 10 ]; do
        [ -n "$(pids_of "$1" 2>/dev/null)" ] || return 0
        j=$((j + 1))
        sleep 1
      done
      echo "[stack] warning: $1 survived SIGKILL"
    fi
    return 0
}

cmd_up() {
  mkdir -p "$DATA"

  # SurrealDB first and alone: the API dials it during startup, so a
  # missing database surfaces as a confusing migration error instead.
  if is_up "surreal start"; then
    echo "surreal  already running"
  else
    # Raw byte counts only (v2 ignores MiB suffixes). 32MB cache + 1 thread
    # keeps RocksDB off llama-server's RAM.
    SURREAL_ROCKSDB_BLOCK_CACHE_SIZE=33554432 \
    SURREAL_HNSW_CACHE_SIZE=33554432 \
    SURREAL_ROCKSDB_WRITE_BUFFER_SIZE=16777216 \
    SURREAL_ROCKSDB_MAX_WRITE_BUFFER_NUMBER=2 \
    SURREAL_ROCKSDB_JOBS_COUNT=2 \
    SURREAL_ROCKSDB_THREAD_COUNT=1 \
      nohup surreal start --user root --pass root "rocksdb:$DATA/test.db" \
      >>"$LOG" 2>&1 &
    echo "surreal  started"
  fi

  # Wait for SurrealDB to actually answer, rather than guessing with a
  # fixed sleep. The sleep 6 this replaces was sized for the dev box and
  # was far too short here: the API dialled the WebSocket endpoint during
  # startup, exhausted its whole retry budget while the server was still
  # binding, and died with "timed out during opening handshake" - which
  # reads like a network fault rather than "you started me too early".
  #
  # Polled inside one python process so the interpreter's import cost is
  # paid once instead of on every attempt; a poll-per-process loop costs
  # more than the wait it is waiting for on this hardware.
  #
  # Best-effort by design: on timeout it warns and continues, leaving the
  # API's own retry loop to cover the residual window rather than refusing
  # to start at all.
  wait_for_surreal() {
    if "$APP/.venv/bin/python" - "${SURREAL_READY_TIMEOUT:-180}" <<'PY'
import sys, time, urllib.request

deadline = time.time() + int(sys.argv[1])
while time.time() < deadline:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read()
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit(1)
PY
    then
      # Here, and nowhere else. The call site used to echo "surreal  ready"
      # unconditionally, so a real timeout printed the WARNING and then
      # "ready" - two contradicting lines from one check, and the false one
      # was the one that got read.
      echo "surreal  ready"
      return 0
    fi
    echo "[stack] WARNING: surreal not ready after ${SURREAL_READY_TIMEOUT:-180}s - starting anyway"
  }

  wait_for_surreal

  cd "$APP" || exit 1

  if is_up "uvicorn api.main:app"; then
    echo "api      already running"
  else
    # The API hosts the dispatcher, so this is where the phone's window
    # ceiling is set. Defaulted here rather than left to the code because a
    # hardware limit that lives only in the repository would be invisible on
    # the one device it exists for.
    OPEN_NOTEBOOK_WINDOW_CAP="${OPEN_NOTEBOOK_WINDOW_CAP:-2}"
    export OPEN_NOTEBOOK_WINDOW_CAP
    # Loopback, not 0.0.0.0. This API serves full unauthenticated job
    # submission to every network the phone joins - a cafe, a hotel, a
    # campus - which is a wider door than any phone application needs.
    # Nothing legitimate requires it: the browser reaches the API through
    # the Next.js proxy on 8502, and the PC reaches it over `adb forward`,
    # which connects to the device's own loopback.
    #
    # OPEN_NOTEBOOK_API_HOST exists so an operator who knowingly wants LAN
    # reach can ask for it, rather than this being an edit to a shipped file.
    api_host="${OPEN_NOTEBOOK_API_HOST:-127.0.0.1}"
    nohup .venv/bin/uvicorn api.main:app --host "$api_host" --port 5055 >>"$LOG" 2>&1 &
    # Confirm it actually bound, instead of printing success and moving on.
    #
    # A stale uvicorn that `down` failed to kill holds 5055, the new process
    # dies immediately with "address already in use", and the launcher
    # reports a clean start - so the device keeps serving the old binary
    # while every check against the wrapper and the repository says the new
    # code is deployed. That is how a loopback binding was believed to be in
    # force for hours on a phone that was still listening on 0.0.0.0.
    if ! wait_for_api 90; then
      report_api_failure 90
      return 1
    fi
    echo "api      started on $api_host (window cap $OPEN_NOTEBOOK_WINDOW_CAP)"
    report_binding "$api_host"
  fi

  if is_up "surreal-commands-worker"; then
    echo "worker   already running"
  else
    # The worker's own limit is a backstop only. Concurrency is decided by
    # the dispatcher, which admits jobs from `held` to `new` up to a window it
    # re-reads every pass - so a window lowered or raised at runtime is not
    # capped by a semaphore that was fixed when this process started. The
    # phone's ceiling of 2 lives in OPEN_NOTEBOOK_WINDOW_CAP, where the
    # dispatcher can apply it: an 8 GB phone with two model servers beside the
    # worker will OOM past that, and a hot phone throttles the CPU away anyway.
    #
    # The backstop must be at least the highest window this device's cap
    # allows, or the semaphore becomes the real limit and the gate governs
    # nothing.
    cap="${OPEN_NOTEBOOK_WINDOW_CAP:-8}"
    case "$cap" in
        ''|*[!0-9]*) cap=8 ;;
    esac
    nohup .venv/bin/surreal-commands-worker --import-modules commands \
      --max-tasks "$cap" >>"$LOG" 2>&1 &
    echo "worker   started (backstop $cap; real window from the dispatcher)"
  fi

  if is_up "next-server"; then
    echo "web      already running"
  else
    cd "$FRONTEND" || exit 1
    # Node comes from the venv (nodejs-wheel-binaries ships an aarch64
    # build); nothing needs installing. HOSTNAME must be 0.0.0.0 or the
    # phone's own browser cannot reach it.
    PORT=8502 HOSTNAME=0.0.0.0 nohup "$NODE" server.js >>"$LOG" 2>&1 &
    echo "web      started"
  fi

  # The guest TTS server (:8092) - piper-plus voices the Termux piper
  # cannot run (espeak-type models; "not a valid PhonemeType"). Optional
  # by design: `onb-stack up` must still work on a phone that never
  # installed it, so the absence is reported rather than failed. The
  # process boots cheap and the model loads on the first request
  # (ADR-014), so a phone that never speaks pays one Python process.
  if [ -x "$TTS_VENV/bin/python" ]; then
    if is_up "termux-guest-tts.py"; then
      echo "tts-ja   already running"
    else
      GUEST_TTS_THREADS="${GUEST_TTS_THREADS:-2}" \
        nohup "$TTS_VENV/bin/python" "$APP/scripts/termux-guest-tts.py" \
        >>"$LOG" 2>&1 &
      echo "tts-ja   started on 8092 (threads ${GUEST_TTS_THREADS:-2}; model loads on first use)"
    fi
  else
    echo "tts-ja   not installed - run onb-tts-ja-install from Termux"
  fi

  sleep 5
  cmd_status
}

cmd_status() {
  for name in "surreal start" "uvicorn api.main:app" "surreal-commands-worker" "next-server"; do
    if pid=$(pid_of "$name"); then
      rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null)
      printf '%-8s up    pid %-7s %s kB\n' "${name%% *}" "$pid" "${rss:-?}"
    else
      printf '%-8s DOWN\n' "${name%% *}"
    fi
  done

  # llama is reported by what it is doing, not by whether a process with
  # its name exists. Those are different facts, and reporting the second
  # one as though it were the first is what let `llama up 3236 kB` sit
  # in this table while chat could not possibly work. A 3 MB process is
  # not a loaded 1.5B model - the loaded one is several hundred MB - so
  # that figure is also printed, as the thing worth noticing.
    # Matched by port, not by executable name. The chat server (:8080) and
    # the embedding server (:8081) are both `llama-server`, so matching the
    # name could return the embedder for the chat row - which is how a
    # healthy stack reported `llama STUCK` while the process it named was
    # serving embeddings perfectly well on the other port.
    if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
      pid=$(pid_of "llama-server --port 8080")
      rss=$(awk '/VmRSS/{print $2}' "/proc/${pid:-0}/status" 2>/dev/null)
      printf '%-8s up    pid %-7s %s kB\n' "llama" "${pid:-?}" "${rss:-?}"
    elif pid=$(pid_of "llama-server --port 8080"); then
      rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null)
      printf '%-8s STUCK pid %-7s %s kB - process present, nothing on :8080\n' \
        "llama" "$pid" "${rss:-?}"
    else
      printf '%-8s DOWN\n' "llama"
    fi

    # The embedder gets its own row. It was absent from this table entirely,
    # which is the same "report what it is doing, not whether a process
    # exists" rule the chat row follows - applied inconsistently. A vector
    # index silently fed by the wrong model is the failure that matters here,
    # and it is invisible unless this row exists.
    if curl -sf -m 2 http://127.0.0.1:8081/health >/dev/null 2>&1; then
      pid=$(pid_of "llama-server --port 8081")
      rss=$(awk '/VmRSS/{print $2}' "/proc/${pid:-0}/status" 2>/dev/null)
      printf '%-8s up    pid %-7s %s kB\n' "embed" "${pid:-?}" "${rss:-?}"
    elif pid=$(pid_of "llama-server --port 8081"); then
      printf '%-8s STUCK pid %-7s - process present, nothing on :8081\n' \
        "embed" "$pid"
    else
      printf '%-8s DOWN\n' "embed"
    fi

    # The guest TTS (:8092), same rule as llama/embed: report what it is
    # doing, not whether a process exists. Its /health also carries
    # model_present, which separates the three real states - serving,
    # process up but model absent, never installed - because each has a
    # different fix and one DOWN line cannot say which.
    _tts_health=$(curl -sf -m 2 http://127.0.0.1:8092/health 2>/dev/null || true)
    if [ -n "$_tts_health" ]; then
      pid=$(pid_of "termux-guest-tts.py")
      rss=$(awk '/VmRSS/{print $2}' "/proc/${pid:-0}/status" 2>/dev/null)
      case "$_tts_health" in
        *'"model_present": false'*)
          printf '%-8s up    pid %-7s %s kB - MODEL MISSING, run onb-tts-ja-install\n' \
            "tts-ja" "${pid:-?}" "${rss:-?}"
          ;;
        *)
          printf '%-8s up    pid %-7s %s kB\n' "tts-ja" "${pid:-?}" "${rss:-?}"
          ;;
      esac
    elif pid=$(pid_of "termux-guest-tts.py"); then
      printf '%-8s STUCK pid %-7s - process present, nothing on :8092\n' \
        "tts-ja" "$pid"
    elif [ ! -x "$TTS_VENV/bin/python" ]; then
      printf '%-8s not installed (onb-tts-ja-install from Termux)\n' "tts-ja"
    else
      printf '%-8s DOWN\n' "tts-ja"
    fi
}

cmd_down() {
  kill_of "next-server"
  wait_gone "next-server"
  kill_of "surreal-commands-worker"
  wait_gone "surreal-commands-worker"
  kill_of "uvicorn api.main:app"
  wait_gone "uvicorn api.main:app"
  kill_of "surreal start"
  wait_gone "surreal start"
    # Both model servers, by executable name: the chat server (:8080) and
    # the embedder (:8081) are the same binary, and a full stop clears both.
    #
    # This was missing entirely, so `down` left the model servers running and
    # a later `up` found :8081 still held - by an embedder serving whatever
    # model it was launched with, hours earlier. It reported "already up"
    # against that server and the wrong model stayed in use. A leftover model
    # server is always wrong, so a full stop clears them by name.
    kill_of "llama-server"
    wait_gone "llama-server"
    # The guest TTS by script name: its argv[0] is the venv python, so
    # the script is the only stable match - the same interpreter-hosted
    # problem the pids_of comment describes. Left behind, it would keep
    # :8092 and a stale model loaded across restarts.
    kill_of "termux-guest-tts.py"
    wait_gone "termux-guest-tts.py"
}

# --- Termux side: delegate; every path above lives inside the guest ---------
#
# Run from ~/bin/onb-stack on the phone, this file used to sail straight
# into guest paths that do not exist in Termux - `cd "$APP"` was where one
# attempt stopped dead - while its earlier steps had already printed along
# the way. An `onb-stack up` from Termux therefore started zero processes
# and reported nothing, which is the failure shape this block exists to
# remove. The guest copy keeps owning all logic; from Termux each verb goes
# where it can actually work.
#
# The gate is the repository's own path, not an argument marker: the
# launcher's `sh .../termux-stack.sh up` and onb-update's `... down` run in
# the guest without one, and a marker they do not pass would turn both into
# a delegation back out of the guest they are already in.
if [ ! -d "$APP" ]; then
  if [ ! -d /data/data/com.termux ]; then
    echo "onb-stack: not in Termux and $APP does not exist here." >&2
    echo "  Run it in Termux as: sh ~/bin/onb-stack up|down|status" >&2
    exit 1
  fi
  if ! command -v proot-distro >/dev/null 2>&1; then
    echo "onb-stack: proot-distro is not installed" >&2
    exit 1
  fi
  if ! command -v timeout >/dev/null 2>&1; then
    echo "onb-stack: timeout (coreutils) not found - refusing an unbounded login" >&2
    exit 1
  fi

  # Bounded the way onb-update's in_guest is, for the reason measured
  # there: proot sets SigIgn bit 15 and discards SIGTERM outright, so a
  # plain `timeout N` sends TERM at N and then waits forever - the bound
  # only terminates with -k. An unbounded login presents as a frozen
  # terminal with no indication of where it is stuck.
  in_guest() {
    if [ "${ONB_GUEST_TIMEOUT:-120}" = "0" ]; then
      proot-distro login ubuntu -- "$@"
      return $?
    fi
    timeout -k "${ONB_GUEST_KILL_GRACE:-5}" "${ONB_GUEST_TIMEOUT:-120}" \
      proot-distro login ubuntu -- "$@"
  }

  # Does the API answer? curl when there is one, otherwise the same /proc
  # scan onb-update's stack_is_up falls back to - this runs in Termux,
  # where the guest's processes sit in our own /proc.
  api_up() {
    if command -v curl >/dev/null 2>&1; then
      curl -s -m 3 -o /dev/null http://127.0.0.1:5055/health 2>/dev/null && return 0
    fi
    for _d in /proc/[0-9]*; do
      _cl=$({ tr '\0' ' ' <"$_d/cmdline"; } 2>/dev/null) || continue
      case "$_cl" in *"uvicorn api.main:app"*) return 0 ;; esac
    done
    return 1
  }

  case "${1:-up}" in
    status)
      # In the guest the venv paths exist, so tts-ja's "down" versus "not
      # installed" is decided against reality instead of against Termux,
      # where no path under /root is visible at all.
      in_guest sh "$APP" status
      exit $?
      ;;
    down)
      # The guest copy kills the services - both model servers included -
      # and prints the table. A launcher parked by an earlier detached
      # start is not a service, so this verb leaves it in place; the next
      # `up` clears it first, because that launcher's already_running
      # guard would otherwise refuse the start outright.
      in_guest sh "$APP" down
      exit $?
      ;;
    up)
      # A healthy stack is reported, never restarted.
      if api_up; then
        echo "onb-stack: already up (5055 answering) - not restarting."
        in_guest sh "$APP" status
        exit $?
      fi
      if [ ! -f "$HOME/bin/onb" ]; then
        echo "onb-stack: launcher ~/bin/onb missing - run termux-install-bin first" >&2
        exit 1
      fi
      # Checked before anything is stopped: discovering it only after the
      # clean slate would leave the device with everything down and no way
      # back up in this same command.
      if ! command -v setsid >/dev/null 2>&1; then
        echo "onb-stack: setsid not found - refusing a start that would stay in" >&2
        echo "  your terminal's process group, where Ctrl+C would take the" >&2
        echo "  stack down with it (proot runs --kill-on-exit)." >&2
        exit 1
      fi
      # Clean slate first, for two measured reasons: a launcher parked by
      # an earlier detached start refuses to start a second time
      # (already_running), and a half-up state - a model server without an
      # API - keeps a port the new start needs. `onb stop` is idempotent
      # against processes that are already dead and sweeps its own stale
      # sessions. Not gating the start on its exit code: a wedged-session
      # timeout there would turn a best-effort cleanup into a false
      # refusal, and the launcher's own guards report what truly cannot
      # be taken - honestly, in the log below.
      if ! sh "$HOME/bin/onb" stop; then
        echo "onb-stack: note: onb stop reported errors clearing the old state;" >&2
        echo "  starting anyway - the launcher's guards report what it cannot take." >&2
      fi
      # Not a direct `proot-distro login ... up`: the login would exit with
      # its command, and --kill-on-exit would reap every service it had just
      # started - a green table, then nothing. And not nohup alone: that
      # only ignores SIGHUP, leaving the launcher in this shell's process
      # group, where Ctrl+C reaches it (onb-update's own lesson). The
      # launcher IS the keep-alive session: it runs this script's `up`
      # inside a guest session that parks forever, and restores the model
      # servers `down` cleared on the way.
      _startlog="$HOME/onb-start.log"
      : > "$_startlog"
      setsid nohup sh "$HOME/bin/onb" >>"$_startlog" 2>&1 </dev/null &
      echo "onb-stack: starting - llama, embed, then the guest stack"
      echo "           detached; about a minute on this device."
      echo "           watch: sh ~/bin/onb-stack status"
      echo "           log:   $_startlog"
      exit 0
      ;;
    *)
      echo "usage: $0 up|down|status" >&2
      exit 1
      ;;
  esac
fi

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) echo "usage: $0 up|down|status"; exit 1 ;;
esac