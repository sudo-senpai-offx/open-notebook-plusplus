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

# Process lookup by cmdline substring. `ss`/`fuser`/`pkill` are not
# installed in this guest, and Next rewrites its own process title to
# "next-server (v16.2.6)" — so match that, never "server.js".
pid_of() {
  for p in /proc/[0-9]*; do
    cl=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
    case "$cl" in
      *"$1"*) echo "${p#/proc/}"; return 0 ;;
    esac
  done
  return 1
}

is_up() { pid_of "$1" >/dev/null 2>&1; }

kill_of() {
  pid=$(pid_of "$1") || return 0
  kill "$pid" 2>/dev/null && echo "stopped $1 (pid $pid)"
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
    sleep 6
  fi

  cd "$APP" || exit 1

  if is_up "uvicorn api.main:app"; then
    echo "api      already running"
  else
    nohup .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 5055 >>"$LOG" 2>&1 &
    echo "api      started"
  fi

  if is_up "surreal-commands-worker"; then
    echo "worker   already running"
  else
    nohup .venv/bin/surreal-commands-worker --import-modules commands \
      --max-tasks 1 >>"$LOG" 2>&1 &
    echo "worker   started"
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
  if pid=$(pid_of "llama-server"); then
    rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status" 2>/dev/null)
    printf '%-8s up    pid %-7s %s kB\n' "llama" "$pid" "${rss:-?}"
  else
    printf '%-8s DOWN\n' "llama"
  fi
}

cmd_down() {
  kill_of "next-server"
  kill_of "surreal-commands-worker"
  kill_of "uvicorn api.main:app"
  kill_of "surreal start"
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) echo "usage: $0 up|down|status"; exit 1 ;;
esac