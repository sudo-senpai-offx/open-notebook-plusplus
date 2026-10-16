#!/bin/sh
# Vehicle 2 stack launcher — run INSIDE the proot Ubuntu guest.
#
# Why this exists: proot is started with --kill-on-exit, so every guest
# process dies when the proot session that spawned it closes. That is how
# SurrealDB "disappeared" mid-session. Nothing started here survives the
# session either — but this script makes bring-up idempotent and cheap
# enough to re-run after any restart, instead of four hand-typed commands
# with a dozen env vars between them.
#
# Usage: sh /root/termux-stack.sh up|down|status
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

# Process lookup by executable, with arguments matched separately.
# `ss`/`fuser`/`pkill` are not installed in this guest, so /proc is the
# only option. Next rewrites its own process title to "next-server
# (v16.2.6)", so the executable name is the stable part to match — never
# "server.js", and never a bare substring of the whole command line.
pids_of() {
  # Every process matching $1, where the pattern is matched in two
  # parts: the executable against argv[0], and any remaining words
  # against the whole command line.
  #
  # This is needed because the callers disagree about what they are
  # matching. "llama-server" and "next-server" are one executable.
  # "uvicorn api.main:app" and "surreal start" are an executable plus
  # its arguments - and for those, argv[0] alone is just `uvicorn`, so
  # an argv[0]-only match would find every uvicorn on the device
  # whatever it was serving.
  #
  # It was a substring match over the whole cmdline, and it lied in a
  # way that mattered: it reported `llama up pid 26053 3236 kB` for a
  # process serving nothing, while termux-launch.sh - with its own copy
  # of this function - correctly reported the port unhealthy. Two
  # implementations of one idea, disagreeing, and the status table was
  # the one that was confidently wrong.
  #
  # Matching argv[0] means a `tail -f llama-server.log` can no longer
  # be mistaken for the server. Returning *all* matches rather than the
  # first is deliberate: llama-server is not reliably a single process,
  # and signalling one of several leaves the port held by another.
  pattern_head=${1%% *}
  pattern_tail=${1#"$pattern_head"}
  pattern_tail=${pattern_tail# }
  for p in /proc/[0-9]*; do
    argv0=$(tr '\0' '\n' 2>/dev/null < "$p/cmdline" | head -1) || continue
    [ -n "$argv0" ] || continue
    case "${argv0##*/}" in
      "$pattern_head") ;;
      *) continue ;;
    esac
    if [ -n "$pattern_tail" ]; then
      tr '\0' ' ' 2>/dev/null < "$p/cmdline" | grep -q -- "$pattern_tail" || continue
    fi
    echo "${p#/proc/}"
  done
}

pid_of() {
  pids_of "$1" | head -1 | grep . || return 1
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
  echo "         survived 'down'. Find and kill it:"
  echo "           proot-distro login ubuntu -- bash -lc 'pkill -f \"uvicorn api.main\"'"
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
  echo "[stack] warning: $1 still present after 20s"
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
      return 0
    fi
    echo "[stack] WARNING: surreal not ready after ${SURREAL_READY_TIMEOUT:-180}s - starting anyway"
  }

  wait_for_surreal
  echo "surreal  ready"

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
  if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    pid=$(pid_of "llama-server")
    rss=$(awk '/VmRSS/{print $2}' "/proc/${pid:-0}/status" 2>/dev/null)
    printf '%-8s up    pid %-7s %s kB\n' "llama" "${pid:-?}" "${rss:-?}"
  elif pid=$(pid_of "llama-server"); then
    rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null)
    printf '%-8s STUCK pid %-7s %s kB - process present, nothing on :8080\n' \
      "llama" "$pid" "${rss:-?}"
  else
    printf '%-8s DOWN\n' "llama"
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
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) echo "usage: $0 up|down|status"; exit 1 ;;
esac