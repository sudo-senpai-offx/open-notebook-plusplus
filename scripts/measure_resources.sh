#!/bin/bash
# Measure resource envelopes of the Open Notebook stack against ADR-009
# Stage 1 limits, then assert them.
#
# Usage: measure_resources.sh [--spawn|--attach] [--smoke] [--node]
#   --spawn   (default) start fresh API + worker, measure cold start + idle
#             RSS, run the ingest->embed smoke task, sample worker task RSS,
#             then tear everything down.
#   --attach  measure already-running processes (API by port, worker/node by
#             pgrep). RSS-only unless --smoke is given.
#   --smoke   (attach mode) also run the ingest->embed smoke against the
#             attached API.
#   --node    include a best-effort node RSS sample (spawns the standalone
#             server if MEASURE_NODE_STANDALONE=1 and a build exists, else
#             measures an already-running next/node process).
#
# Assertions are per-metric PASS/FAIL (never set -e). Exit code is non-zero
# when any metric fails. Dev/CI tooling only - ships in neither the runtime
# image nor the product image.
#
# Config (env, defaults follow ADR-009 Stage 1):
#   MEASURE_API_RSS_LIMIT_MB         220
#   MEASURE_WORKER_IDLE_RSS_LIMIT_MB 300
#   MEASURE_WORKER_TASK_RSS_LIMIT_MB 500
#   MEASURE_NODE_RSS_LIMIT_MB        150
#   MEASURE_API_START_LIMIT_S        5
#   MEASURE_WORKER_START_LIMIT_S     30
#   MEASURE_SMOKE_BUDGET_S           120
#   MEASURE_API_PORT                 5091   (spawn mode API port)
#   MEASURE_NODE_STANDALONE          0      (spawn node standalone server)
#   MEASURE_SURREAL_ADDRESS          127.0.0.1 (DB address for spawned procs)
#   MEASURE_SURREAL_PORT             8108   (DB port for spawned procs)
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
MODE="${1:---spawn}"
[ "$MODE" = "--spawn" ] || [ "$MODE" = "--attach" ] || { echo "usage: $0 [--spawn|--attach] [--smoke] [--node]" >&2; exit 2; }
shift || true
SMOKE=0; NODE_ON=0
for a in "$@"; do case "$a" in --smoke) SMOKE=1 ;; --node) NODE_ON=1 ;; *) echo "unknown flag: $a" >&2; exit 2 ;; esac; done

# --- limits / knobs ---------------------------------------------------------
API_RSS_LIMIT=${MEASURE_API_RSS_LIMIT_MB:-220}
WKR_IDLE_LIMIT=${MEASURE_WORKER_IDLE_RSS_LIMIT_MB:-300}
WKR_TASK_LIMIT=${MEASURE_WORKER_TASK_RSS_LIMIT_MB:-500}
NODE_LIMIT=${MEASURE_NODE_RSS_LIMIT_MB:-150}
API_START_LIMIT=${MEASURE_API_START_LIMIT_S:-5}
WKR_START_LIMIT=${MEASURE_WORKER_START_LIMIT_S:-30}
SMOKE_BUDGET=${MEASURE_SMOKE_BUDGET_S:-120}
API_PORT=${MEASURE_API_PORT:-5091}
NODE_STANDALONE=${MEASURE_NODE_STANDALONE:-0}
DB_ADDR="${MEASURE_SURREAL_ADDRESS:-127.0.0.1}"
DB_PORT="${MEASURE_SURREAL_PORT:-8108}"
API="http://127.0.0.1:$API_PORT"

PASS=0; FAIL=0
ok()    { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()   { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn()  { echo "  ⚠️  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected=$2, got=$3)"; fi; }

rss_mb() { # <pid> -> RSS in MB (best effort, 0 if gone)
  [ -r "/proc/$1/status" ] || { echo 0; return; }
  awk '/^VmRSS:/ { printf "%d", $2/1024 }' "/proc/$1/status"
}
ms_now() { date +%s%3N; }

resolve_child() { # <wrapper pid> - actual process the `uv run` wrapper spawned
  pgrep -P "$1" 2>/dev/null | head -1
}
attach_api_pid() { # deepest non-uv process running the api
  for p in $(pgrep -f "uvicorn api\.main:app" 2>/dev/null; pgrep -f "run_api\.py" 2>/dev/null); do
    [ "$(ps -o comm= -p "$p" 2>/dev/null)" != "uv" ] && { echo "$p"; break; }
  done
}
attach_wkr_pid() {
  for p in $(pgrep -f "surreal-commands-worker" 2>/dev/null); do
    [ "$(ps -o comm= -p "$p" 2>/dev/null)" != "uv" ] && { echo "$p"; break; }
  done
}
find_node_pid() {
  local p=$(pgrep -f "next dev|node server\.js|next-server" 2>/dev/null | head -1)
  if [ -z "$p" ] && command -v ss >/dev/null 2>&1; then
    p=$(ss -ltnp 2>/dev/null | awk '/:8599 / && /pid=/ { match($0, /pid=[0-9]+/); pid=substr($0, RSTART+4, RLENGTH-4); print pid }' | head -1)
  fi
  echo "$p"
}
kill_tree() { # <wrapper pid> <resolved pid> - kill the whole spawned tree
  [ -n "${2:-}" ] && kill "$2" 2>/dev/null
  [ -n "${1:-}" ] && kill "$1" 2>/dev/null
  [ -n "${1:-}" ] && [ -n "${2:-}" ] && { sleep 1; kill "$2" 2>/dev/null; kill -9 "$1" 2>/dev/null; }
}

WORK="$(mktemp -d /tmp/measure-resources.XXXXXX)"
API_PID=""; WKR_PID=""; NODE_PID=""; API_WRAP=""; WKR_WRAP=""; NODE_WRAP=""
API_LOG="$WORK/api.log"; WKR_LOG="$WORK/worker.log"; NODE_LOG="$WORK/node.log"
cleanup() {
  [ -n "${NODE_PID:-}" ] && kill "$NODE_PID" 2>/dev/null
  # graceful worker stop so SurrealDB LIVE queries release cleanly
  [ -n "${WKR_PID:-}" ] && sleep 1 && kill "${WKR_PID}" 2>/dev/null
  kill_tree "${API_WRAP:-}" "${API_PID:-}"
  kill_tree "${WKR_WRAP:-}" "${WKR_PID:-}"
  kill_tree "${NODE_WRAP:-}" "${NODE_PID:-}"
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

# --- preflight --------------------------------------------------------------
echo "== Preflight =="
command -v curl >/dev/null 2>&1 || { echo "curl required" >&2; exit 2; }
if [ "$MODE" = "--spawn" ]; then
  command -v uv >/dev/null 2>&1 || { echo "uv required in spawn mode" >&2; exit 2; }
  if ! curl -sf -m 2 "http://${DB_ADDR}:${DB_PORT}/health" >/dev/null 2>&1 && \
     ! (echo "" | docker exec -i surrealdb-debug /surreal sql --endpoint "http://127.0.0.1:${DB_PORT}" --username root --password root -q "RETURN 1;" >/dev/null 2>&1); then
    bad "SurrealDB reachable (${DB_ADDR}:${DB_PORT})"
  else
    ok "SurrealDB reachable (${DB_ADDR}:${DB_PORT})"
  fi
fi

# --- spawn mode -------------------------------------------------------------
if [ "$MODE" = "--spawn" ]; then
  echo "== Spawning API + worker (listening on 127.0.0.1:$API_PORT) =="
  export SURREAL_ADDRESS="$DB_ADDR" SURREAL_PORT="$DB_PORT"
  export OPEN_NOTEBOOK_PASSWORD=""

  START=$(ms_now)
  uv run --env-file .env uvicorn api.main:app --host 127.0.0.1 --port "$API_PORT" >"$API_LOG" 2>&1 &
  API_WRAP=$!
  unset API_PID
  echo "  API wrapper pid=$API_WRAP"

  API_START_MS=""
  for i in $(seq 1 40); do
    if curl -sf -m 2 -o /dev/null "$API/health"; then
      API_PID=$(resolve_child "$API_WRAP")
      API_START_MS=$(( $(ms_now) - START )); break
    fi
    sleep 0.5
  done
  if [ -n "$API_START_MS" ]; then
    API_START_S=$(awk -v ms="$API_START_MS" 'BEGIN { printf "%.2f", ms/1000 }')
    ok "API cold start: ${API_START_S}s"
  else
    API_START_S="n/a"; API_PID=""; bad "API cold start (never became healthy)"
  fi

  START=$(ms_now)
  uv run --env-file .env surreal-commands-worker --import-modules commands --max-tasks 1 >"$WKR_LOG" 2>&1 &
  WKR_WRAP=$!
  unset WKR_PID
  echo "  worker wrapper pid=$WKR_WRAP"

  WKR_START_MS=""
  for i in $(seq 1 180); do
    if grep -q "configured to handle up to\|Starting LIVE query listener" "$WKR_LOG" 2>/dev/null; then
      WKR_PID=$(resolve_child "$WKR_WRAP")
      WKR_START_MS=$(( $(ms_now) - START )); break
    fi
    if ! kill -0 "$WKR_WRAP" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if [ -n "$WKR_START_MS" ]; then
    WKR_START_S=$(awk -v ms="$WKR_START_MS" 'BEGIN { printf "%.2f", ms/1000 }')
    ok "worker cold start: ${WKR_START_S}s"
  else
    WKR_START_S="n/a"; WKR_PID=""; bad "worker cold start (never became ready)"
  fi
  sleep 2  # let both settle before sampling idle RSS
fi

# --- attach mode ------------------------------------------------------------
if [ "$MODE" = "--attach" ]; then
  echo "== Attaching to running processes =="
  API_PID=$(attach_api_pid)
  WKR_PID=$(attach_wkr_pid)
  ATTACH_PORT="${MEASURE_API_PORT:-}"
  if [ -z "$ATTACH_PORT" ]; then
    ATTACH_PORT=$(command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '/:5055 / {print $4}' | grep -oE '[0-9]+$' | head -1)
  fi
  ATTACH_PORT="${ATTACH_PORT:-5055}"
  API="http://127.0.0.1:$ATTACH_PORT"
  [ -n "$API_PID" ] && ok "API attached (pid $API_PID)" || warn "no running API found"
  [ -n "$WKR_PID" ] && ok "worker attached (pid $WKR_PID)" || warn "no running worker found"
fi

# --- idle RSS ---------------------------------------------------------------
echo "== Idle RSS =="
if [ -n "$API_PID" ]; then
  API_RSS=$(rss_mb "$API_PID")
  check "API idle RSS < ${API_RSS_LIMIT}MB" 1 "$([ "$API_RSS" -gt 0 ] && [ "$API_RSS" -lt "$API_RSS_LIMIT" ] && echo 1 || echo 0)"
  echo "    (API RSS: ${API_RSS}MB)"
else
  warn "API RSS skipped (no process)"
fi
if [ -n "$WKR_PID" ]; then
  WKR_IDLE=$(rss_mb "$WKR_PID")
  check "worker idle RSS < ${WKR_IDLE_LIMIT}MB" 1 "$([ "$WKR_IDLE" -gt 0 ] && [ "$WKR_IDLE" -lt "$WKR_IDLE_LIMIT" ] && echo 1 || echo 0)"
  echo "    (worker idle RSS: ${WKR_IDLE}MB)"
else
  warn "worker RSS skipped (no process)"
fi

# --- node RSS (best effort) -------------------------------------------------
if [ "$NODE_ON" = "1" ] || [ "$MODE" = "--attach" ] || { [ "$MODE" = "--spawn" ] && [ "$NODE_STANDALONE" = "1" ]; }; then
  echo "== Node RSS (best effort) =="
  NODE_PID=$(find_node_pid)
  if [ -z "$NODE_PID" ] && [ "$MODE" = "--spawn" ] && [ "$NODE_STANDALONE" = "1" ] && [ -f "$ROOT/frontend/.next/standalone/server.js" ]; then
    ( cd "$ROOT/frontend/.next/standalone" && HOSTNAME=127.0.0.1 PORT=8599 node server.js >"$NODE_LOG" 2>&1 & echo $! > "$WORK/node.pid" )
    NODE_WRAP=$(cat "$WORK/node.pid" 2>/dev/null)
    for i in $(seq 1 15); do   # wait until actually listening (server renames to next-server)
      NODE_PID=$(find_node_pid)
      [ -n "$NODE_PID" ] && break
      sleep 1
    done
    sleep 2  # let RSS settle after boot
  fi
  NODE_RSS=0
  [ -n "$NODE_PID" ] && NODE_RSS=$(rss_mb "$NODE_PID")
  if [ "$NODE_RSS" -gt 0 ]; then
    check "node RSS < ${NODE_LIMIT}MB" 1 "$([ "$NODE_RSS" -lt "$NODE_LIMIT" ] && echo 1 || echo 0)"
    echo "    (node RSS: ${NODE_RSS}MB, pid $NODE_PID)"
  else
    warn "node RSS skipped (no node process / no standalone build)"
  fi
fi

# --- smoke ingest->embed ----------------------------------------------------
if [ "$SMOKE" = "1" ] || [ "$MODE" = "--spawn" ]; then
  echo "== Smoke: ingest -> embed =="
  if ! curl -sf -m 3 -o /dev/null "$API/health"; then
    bad "smoke requires a healthy API (none at $API)"
  else
    TS=$(date +%s)
    NB=$(curl -sf -X POST "$API/api/notebooks" -H 'Content-Type: application/json' \
      -d "{\"name\":\"measure-$TS\",\"description\":\"resource harness\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "")
    if [ -z "$NB" ]; then
      bad "create harness notebook"
    else
      ok "created harness notebook $NB"
      SRC=$(curl -sf -X POST "$API/api/sources" \
        -F "type=text" \
        -F "notebook_id=$NB" \
        -F "title=resource harness smoke $(date -u +%s)" \
        -F "content=Open Notebook is a research environment for gathering, organizing, and understanding information. It provides notebooks, sources, transformations, and AI-powered chat. This is a smoke payload used by the resource measurement harness to exercise the ingest to embedding pipeline end to end within a time budget. The text is intentionally short so the embedding step dominates." \
        -F "embed=true" \
        -F "async_processing=true" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])' 2>/dev/null || echo "")
      if [ -z "$SRC" ]; then
        bad "submit smoke source"
      else
        ok "submitted smoke source $SRC"
        BUDGET_START=$(ms_now)
        DONE=0; PEAK=0
        while [ $(( $(ms_now) - BUDGET_START )) -lt $((SMOKE_BUDGET*1000)) ]; do
          if [ -n "$WKR_PID" ]; then
            W=$(rss_mb "$WKR_PID"); [ "$W" -gt "$PEAK" ] && PEAK=$W
          fi
          ST=$(curl -sf -m 3 "$API/api/sources/$SRC/status" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")' 2>/dev/null)
          if [ "$ST" = "completed" ]; then
            EMB=$(curl -sf -m 3 "$API/api/sources/$SRC" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("embedded"))' 2>/dev/null)
            if [ "$EMB" = "True" ] || [ "$EMB" = "true" ]; then DONE=1; fi
          fi
          [ "$DONE" = "1" ] && break
          sleep 1
        done
        if [ "$DONE" = "1" ]; then
          ok "smoke ingest->embed completed within ${SMOKE_BUDGET}s"
        else
          bad "smoke ingest->embed did not complete within ${SMOKE_BUDGET}s (last status: ${ST:-unknown})"
        fi
        [ "$PEAK" -gt 0 ] && { echo "    (worker task peak RSS: ${PEAK}MB)"; check "worker task RSS < ${WKR_TASK_LIMIT}MB" 1 "$([ "$PEAK" -lt "$WKR_TASK_LIMIT" ] && echo 1 || echo 0)"; }
      fi
    fi
  fi
fi

# --- enforce startup limits explicitly (kept separate for readability) ------
if [ "$MODE" = "--spawn" ]; then
  if [ -n "$API_START_MS" ]; then
    awk -v ms="$API_START_MS" -v lim="$API_START_LIMIT" 'BEGIN { if (ms/1000 >= lim) exit 1 }' \
      && ok "API startup < ${API_START_LIMIT}s" || bad "API startup >= ${API_START_LIMIT}s"
  fi
  if [ -n "$WKR_START_MS" ]; then
    awk -v ms="$WKR_START_MS" -v lim="$WKR_START_LIMIT" 'BEGIN { if (ms/1000 >= lim) exit 1 }' \
      && ok "worker startup < ${WKR_START_LIMIT}s" || bad "worker startup >= ${WKR_START_LIMIT}s"
  fi
fi

echo
echo "== RESULT: $PASS passed, $FAIL failed =="
[ "$FAIL" -gt 0 ] && exit 1 || exit 0