#!/data/data/com.termux/files/usr/bin/sh
# Progress monitor for the insight regeneration. Read-only by construction.
#
#   sh scripts/monitor-insights.sh [interval_seconds]
#
# Writes one line per tick to notebook_data/insight-progress.log and exits when
# the run is finished, so a long drain does not need anyone watching it.
#
# Read-only on purpose. Restarting the app container kills in-flight jobs as
# "stale: no heartbeat" and discards up to 40 minutes of generation each time,
# so nothing in here may restart, rebuild or promote anything.

set -u

INTERVAL="${1:-300}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
LOG="$ROOT/notebook_data/insight-progress.log"
API="http://127.0.0.1:5055"

# Three transformations across every source that has text. One source has none,
# so the ceiling is 45 rather than 48.
EXPECTED=45

PY="$ROOT/.venv/bin/python"

query() {
  cd "$ROOT" || exit 1
  set -a && . ./.env && set +a
  export SURREAL_URL="${SURREAL_URL:-ws://127.0.0.1:8108/rpc}"
  "$PY" - "$1" <<'EOF'
import asyncio, sys
sys.path.insert(0, ".")
from open_notebook.database.repository import repo_query

WANTED = {"Dense Summary", "Key Insights", "Reflection Questions"}

async def main():
    kind = sys.argv[1]
    if kind == "counts":
        rows = await repo_query(
            "SELECT insight_type, count() AS n FROM source_insight GROUP BY insight_type"
        )
        by = {str(r.get("insight_type")): int(r.get("n") or 0) for r in rows}
        good = sum(v for k, v in by.items() if k in WANTED)
        stale = sum(v for k, v in by.items() if k not in WANTED)
        print(f"{good} {stale} " + " ".join(f"{k}={v}" for k, v in sorted(by.items())))
    elif kind == "pending":
        rows = await repo_query(
            "SELECT name, status, count() AS n FROM command "
            "WHERE status IN ['held', 'running', 'new'] GROUP BY name, status"
        )
        parts = [f"{r.get('name')}/{r.get('status')}={r.get('n')}" for r in rows]
        print(" ".join(parts) if parts else "none")

asyncio.run(main())
EOF
}

api_queue() {
  # Unused on purpose. The authoritative tally comes from the same query the
  # scheduler reads; a second source of truth would be a second thing to be
  # wrong, and "the queue says zero while the insights table says twenty" is
  # exactly the ambiguity this monitor exists to remove.
  password=$(grep '^OPEN_NOTEBOOK_PASSWORD=' "$ROOT/.env" | cut -d= -f2-)
  curl -s -m 8 -H "Authorization: Bearer $password" "$API/api/processing/status" 2>/dev/null \
    || echo "unavailable"
}

tick=0
while [ "$tick" -lt 400 ]; do
  counts=$(query counts 2>/dev/null || echo "0 0 error")
  pending=$(query pending 2>/dev/null || echo "unavailable")
  good=$(printf '%s' "$counts" | cut -d' ' -f1)
  stale=$(printf '%s' "$counts" | cut -d' ' -f2)
  detail=$(printf '%s' "$counts" | cut -d' ' -f3-)
  stamp=$(date '+%H:%M:%S')
  line="$stamp  good=$good/$EXPECTED stale=$stale | $detail | $pending"
  printf '%s\n' "$line" | tee -a "$LOG"

  if [ "$good" -ge "$EXPECTED" ] 2>/dev/null; then
    printf '%s  DONE: %s of %s insights written.\n' "$stamp" "$good" "$EXPECTED" | tee -a "$LOG"
    exit 0
  fi
  case "$pending" in
    *unavailable* | "" | none)
      printf '%s  Nothing pending. Stopping.\n' "$stamp" | tee -a "$LOG"
      exit 0
      ;;
  esac

  tick=$((tick + 1))
  sleep "$INTERVAL"
done

printf '  Gave up after %s ticks.\n' "$tick" | tee -a "$LOG"