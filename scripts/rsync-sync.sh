#!/bin/sh
# Content sync over rsync+SSH — the PC-side driver for Track B (E18 P1).
#
# Replaces git push/pull with rsync over SSH for manifest transfer.
# The API owns all export/import semantics, conflict detection, and dry-run.
# This script only moves files; blobs stay on the existing HTTP channel.
#
#   sh scripts/rsync-sync.sh init <ssh-remote>   one-time: store remote spec
#   sh scripts/rsync-sync.sh push                export -> rsync to remote
#   sh scripts/rsync-sync.sh pull                rsync from remote -> dry-run import
#   sh scripts/rsync-sync.sh pull --apply        import for real
#   sh scripts/rsync-sync.sh cycle               pull --apply, then push
#   sh scripts/rsync-sync.sh blobs <peer> pull   fetch missing file bytes from peer
#   sh scripts/rsync-sync.sh blobs <peer> push   offer our blobs to peer
#   sh scripts/rsync-sync.sh auto                one auto-sync cycle (dry-run import)
#   sh scripts/rsync-sync.sh conflicts           list pending conflicts via API
#   sh scripts/rsync-sync.sh status              show sync dir, remote, manifest counts
#
# Why rsync instead of git:
#   * Delta-based transfer: only changed blocks move, not whole commits.
#   * No git history needed — the manifest is a snapshot of current state.
#   * Works wherever SSH+rsync are available; no git server required.
#
# Why not Syncthing for this script:
#   Syncthing is continuous and backgrounded (see syncthing/ directory).
#   rsync-sync.sh is operator-initiated and explicit. They are alternatives,
#   not layers.

set -u

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
API="${SYNC_API:-http://127.0.0.1:5055}"
SYNC_DIR="${SYNC_DIR:-$REPO/sync}"
SYNC_DEVICE="${SYNC_DEVICE:-pc}"
TOKEN_FILE="${TOKEN_FILE:-$REPO/.env}"
SYNC_RC="${SYNC_RC:-$REPO/.sync-rc}"
RSYNC_SSH_OPTS="${RSYNC_SSH_OPTS:--o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

die() { echo "  ERROR: $*" >&2; exit 1; }

# --- environment ----------------------------------------------------------

# Source .sync-rc if it exists (RSYNC_REMOTE lives there).
if [ -f "$SYNC_RC" ]; then
  # shellcheck disable=SC1090
  . "$SYNC_RC"
fi

api_up() {
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] || die "the app is not answering on $API (HTTP $code).
         Start it first."
}

auth_header() {
  tok=$(sync_token)
  [ -n "$tok" ] && printf 'Authorization: Bearer %s' "$tok"
}

sync_token() {
  [ -f "$TOKEN_FILE" ] || return 0
  sed -n 's/^OPEN_NOTEBOOK_SYNC_TOKEN=//p' "$TOKEN_FILE" | tail -1 | tr -d '"' | tr -d '\r'
}

api_password() {
  [ -f "$TOKEN_FILE" ] || return 0
  sed -n 's/^OPEN_NOTEBOOK_PASSWORD=//p' "$TOKEN_FILE" | tail -1 | tr -d '"' | tr -d '\r'
}

password_header() {
  pw=$(api_password)
  [ -n "$pw" ] && printf 'Authorization: Bearer %s' "$pw"
}

check_tools() {
  command -v rsync >/dev/null 2>&1 || die "rsync is not installed"
  command -v ssh >/dev/null 2>&1 || die "ssh is not installed"
  [ -n "${RSYNC_REMOTE:-}" ] || die "RSYNC_REMOTE is not set.
         Run:  sh scripts/rsync-sync.sh init user@host:/path/to/sync
         or set RSYNC_REMOTE in $SYNC_RC or as an env var."
}

# --- commands -------------------------------------------------------------

cmd_init() {
  remote="${1:-}"
  [ -n "$remote" ] || die "usage: rsync-sync.sh init <ssh-remote>
         e.g.  rsync-sync.sh init deploy@host:/home/deploy/open-notebook/sync"
  check_tools
  mkdir -p "$SYNC_DIR" || die "cannot create $SYNC_DIR"

  # Persist the remote spec.
  printf 'RSYNC_REMOTE=%s\n' "$remote" > "$SYNC_RC"
  chmod 600 "$SYNC_RC" 2>/dev/null || true

  # Verify SSH reachability before declaring success.
  host_part="${remote%%:*}"
  path_part="${remote#*:}"
  if [ "$host_part" = "$remote" ]; then
    die "remote must include a path: user@host:/path/to/sync"
  fi
  if ! ssh $RSYNC_SSH_OPTS "$host_part" true 2>/dev/null; then
    die "cannot reach $host_part over SSH. Check keys and BatchMode."
  fi
  # Make sure the remote path exists and is writable.
  ssh $RSYNC_SSH_OPTS "$host_part" "mkdir -p '$path_part'" 2>/dev/null ||
    die "cannot create $path_part on $host_part"

  echo "  sync dir : $SYNC_DIR"
  echo "  remote   : $remote"
  echo "  stored in: $SYNC_RC"
  echo "  run 'sh scripts/rsync-sync.sh push' to export and send."
}

cmd_push() {
  api_up
  check_tools
  mkdir -p "$SYNC_DIR" || die "cannot create $SYNC_DIR"

  echo "[rsync] exporting from the app (device: $SYNC_DEVICE)"
  pw=$(api_password)
  curl -s -m 300 -X POST "$API/api/sync/export" \
    ${pw:+-H "Authorization: Bearer $pw"} \
    -H 'Content-Type: application/json' \
    -d "{\"device\":\"$SYNC_DEVICE\"}" |
    grep -o '"counts":{[^}]*}' || echo "  (no counts in response - check the app log)"

  # Validate that our SYNC_DIR matches the API's view before transporting.
  api_sync_dir=$(curl -s -m 10 ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/status" 2>/dev/null \
    | grep -o '"sync_dir":"[^"]*"' | head -1 | sed 's/"sync_dir":"//;s/"//')
  if [ -n "$api_sync_dir" ] && [ "$(cd "$SYNC_DIR" 2>/dev/null && pwd -P)" != "$(cd "$api_sync_dir" 2>/dev/null && pwd -P)" ]; then
    die "SYNC_DIR ($SYNC_DIR) does not match the API's sync_dir ($api_sync_dir).
         Set SYNC_DIR to the same absolute path the API uses."
  fi

  echo "[rsync] pushing manifest to $RSYNC_REMOTE"
  # shellcheck disable=SC2086
  rsync -a --update --delete -e "ssh $RSYNC_SSH_OPTS" "$SYNC_DIR/" "$RSYNC_REMOTE/" ||
    die "rsync push failed"
  echo "[rsync] pushed"
}

cmd_pull() {
  apply=false
  [ "${1:-}" = "--apply" ] && apply=true
  check_tools
  [ -d "$SYNC_DIR" ] || die "no sync dir at $SYNC_DIR - run: rsync-sync.sh init <ssh-remote>"

  echo "[rsync] pulling manifest from $RSYNC_REMOTE"
  # shellcheck disable=SC2086
  rsync -a --update -e "ssh $RSYNC_SSH_OPTS" "$RSYNC_REMOTE/" "$SYNC_DIR/" ||
    die "rsync pull failed"

  api_up

  echo "[rsync] import dry run (nothing written yet)"
  pw=$(api_password)
  curl -s -m 300 -X POST "$API/api/sync/import" \
    ${pw:+-H "Authorization: Bearer $pw"} \
    -H 'Content-Type: application/json' -d '{"dry_run":true}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog|queued|retry|errors' | sed 's/^/       /'

  if [ "$apply" != "true" ]; then
    echo
    echo "  Dry run only. Re-run with --apply to write:"
    echo "       sh scripts/rsync-sync.sh pull --apply"
    return 0
  fi

  echo "[rsync] importing"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    ${pw:+-H "Authorization: Bearer $pw"} \
    -H 'Content-Type: application/json' -d '{"dry_run":false}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog|queued|retry|errors' | sed 's/^/       /'
  echo "[rsync] done"
}

cmd_cycle() {
  cmd_pull --apply && cmd_push
}

cmd_status() {
  check_tools
  [ -d "$SYNC_DIR" ] || die "no sync dir at $SYNC_DIR"
  echo "  device  : $SYNC_DEVICE"
  echo "  dir     : $SYNC_DIR"
  echo "  remote  : ${RSYNC_REMOTE:-'(not set - run init)'}"
  echo "  head    : $(ls -t "$SYNC_DIR" 2>/dev/null | head -1 | xargs -I{} stat -c '%y %n' "$SYNC_DIR"/{} 2>/dev/null | head -1 || echo '(empty)')"
  echo "  exported manifest:"
  if [ -f "$SYNC_DIR/manifest.json" ]; then
    grep -o '"counts":{[^}]*}' "$SYNC_DIR/manifest.json" | sed 's/^/       /'
    grep -o '"sources_awaiting_blobs":[0-9]*' "$SYNC_DIR/manifest.json" | sed 's/^/       /'
  else
    echo "       (nothing exported yet)"
  fi
  echo "  app sync status:"
  pw=$(api_password)
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/status" 2>/dev/null) || code=000
  if [ "$code" = "200" ]; then
    curl -s -m 10 ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/status" | tr -d ' \n'
    echo
  else
    echo "       not answering (HTTP $code)"
  fi
}

cmd_blobs() {
  peer="${1:-}"; action="${2:-}"
  [ -n "$peer" ] || die "usage: rsync-sync.sh blobs <peer-url> pull|push"
  api_up
  hdr=$(auth_header)
  phdr=$(password_header)
  [ -z "$hdr" ] && echo "  note: no sync token found in $TOKEN_FILE - the peer will refuse if it requires one"

  case "$action" in
    pull)
      echo "[blobs] pulling from $peer"
      curl -s -m 900 -X POST "$API/api/sync/blobs/pull" ${hdr:+-H "$hdr"} ${phdr:+-H "$phdr"} \
        -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
        tr ',' '\n' | grep -E 'requested|fetched|failed' | sed 's/^/       /'
      ;;
    push)
      echo "[blobs] pushing to $peer"
      curl -s -m 900 -X POST "$API/api/sync/blobs/push" ${hdr:+-H "$hdr"} ${phdr:+-H "$phdr"} \
        -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
        tr ',' '\n' | grep -E 'local|sent|failed' | sed 's/^/       /'
      ;;
    *) die "action must be pull or push" ;;
  esac
}

cmd_auto() {
  api_up
  check_tools
  [ -d "$SYNC_DIR" ] || die "no sync dir at $SYNC_DIR - run: rsync-sync.sh init <ssh-remote>"

  echo "[auto] exporting from the app (device: $SYNC_DEVICE)"
  pw=$(api_password)
  curl -s -m 300 -X POST "$API/api/sync/export" \
    ${pw:+-H "Authorization: Bearer $pw"} \
    -H 'Content-Type: application/json' \
    -d "{\"device\":\"$SYNC_DEVICE\"}" |
    grep -o '"counts":{[^}]*}' || echo "  (no counts in response - check the app log)"

  echo "[auto] pulling"
  # shellcheck disable=SC2086
  rsync -a --update -e "ssh $RSYNC_SSH_OPTS" "$RSYNC_REMOTE/" "$SYNC_DIR/" ||
    die "rsync pull failed"

  echo "[auto] import dry-run (conflict detection)"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    ${pw:+-H "Authorization: Bearer $pw"} \
    -H 'Content-Type: application/json' -d '{"dry_run":true}' |
    tr ',' '\n' | grep -E 'created|updated|stale|errors' | sed 's/^/       /'

  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/conflicts" 2>/dev/null) || code=000
  if [ "$code" = "200" ]; then
    conflicts=$(curl -s -m 10 ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/conflicts" 2>/dev/null)
    if echo "$conflicts" | grep -q '"resolved":false'; then
      echo
      echo "[auto] CONFLICTS DETECTED - review with:"
      echo "       rsync-sync.sh conflicts   # list"
    else
      echo "[auto] no pending conflicts"
    fi
  fi

  echo "[auto] cycle complete"
}

cmd_conflicts() {
  api_up
  pw=$(api_password)
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/conflicts" 2>/dev/null) || code=000
  if [ "$code" != "200" ]; then
    echo "  API not answering (HTTP $code)"
    return 1
  fi
  curl -s -m 10 ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/conflicts" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    print('  (unable to parse conflict list)')
    sys.exit(0)
if not data:
    print('  no pending conflicts')
else:
    for c in data:
        print(f\"  {c['collection']}:{c['db_record_id']}\")
        print(f\"    local_updated : {c.get('local_updated')}\")
        print(f\"    remote_updated: {c.get('remote_updated')}\")
        print(f\"    resolved      : {c.get('resolved')}\")
" 2>/dev/null || curl -s -m 10 ${pw:+-H "Authorization: Bearer $pw"} "$API/api/sync/conflicts" 2>/dev/null
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  push) cmd_push ;;
  pull) shift; cmd_pull "${1:-}" ;;
  cycle) cmd_cycle ;;
  status) cmd_status ;;
  blobs) shift; cmd_blobs "$@" ;;
  auto) cmd_auto ;;
  conflicts) cmd_conflicts ;;
  *)
    sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
