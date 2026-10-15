#!/bin/sh
# Content sync for the PC side of an Open Notebook pair.
#
#   sh scripts/pc-sync.sh init [remote-url]  one-time: make the repo
#   sh scripts/pc-sync.sh push                export -> commit -> push
#   sh scripts/pc-sync.sh pull                pull  -> import (dry run)
#   sh scripts/pc-sync.sh pull --apply        import for real
#   sh scripts/pc-sync.sh cycle               pull --apply, then push
#   sh scripts/pc-sync.sh blobs <peer> push   send file bytes to the peer
#   sh scripts/pc-sync.sh bundle <path>       write a git bundle (offline)
#   sh scripts/pc-sync.sh fetch-bundle <path> read one back
#   sh scripts/pc-sync.sh status
#
# The phone has `termux-sync.sh`, which drives the same three endpoints
# from Termux. This is the PC's counterpart, and the pairing is
# deliberate: neither script reads the database. Both ask the API what to
# export and what to import, so what travels is whatever the API decided
# - there is no second, drifting idea of what the data is. A driver that
# queried SurrealDB itself would agree with the API today and diverge the
# first time an export rule changed.
#
# Two routes to the same repository:
#
#   online   a private git remote. Both devices reach GitHub, which is
#            the only topology where the phone can pull without the PC
#            bridging for it - it cannot open a TCP connection to the PC
#            on the LAN.
#
#   offline  a git bundle carried over adb. Needs no network at either
#            end, and is the fallback when there is no internet. Blobs
#            still need the tunnel: bundle carries the manifest, not the
#            bytes.

set -u

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
API="${SYNC_API:-http://127.0.0.1:5055}"
# The API resolves its sync dir relative to its own working directory,
# which is the repository root, so this must agree with it rather than
# being a second opinion about where the data lives.
SYNC_DIR="${SYNC_DIR:-$REPO/sync}"
DEVICE="${SYNC_DEVICE:-pc}"
TOKEN_FILE="${TOKEN_FILE:-$REPO/.env}"

die() { echo "  ERROR: $*" >&2; exit 1; }

api_up() {
  # `$(... || echo 000)` prints curl's own 000 *and* the fallback, giving
  # 000000. Assign then override instead.
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] || die "the app is not answering on $API (HTTP $code).
         Start it first:  sh scripts/termux-launch.sh up"
}

# git needs an identity even for a one-file repository, and a fresh
# checkout has none.
git_sane() {
  git -C "$SYNC_DIR" config user.email >/dev/null 2>&1 ||
    git -C "$SYNC_DIR" config user.email "sync@localhost"
  git -C "$SYNC_DIR" config user.name >/dev/null 2>&1 ||
    git -C "$SYNC_DIR" config user.name "open-notebook-sync"
}

git_changes() { [ -n "$(git -C "$SYNC_DIR" status --porcelain 2>/dev/null)" ]; }

sync_token() {
  # Read from .env rather than exporting in the caller's shell: an export
  # in a Termux session does not reach the API process, which is the
  # reason this has bitten before.
  [ -f "$TOKEN_FILE" ] || return 0
  sed -n 's/^OPEN_NOTEBOOK_SYNC_TOKEN=//p' "$TOKEN_FILE" | tail -1 | tr -d '"'"'"'\r'
}

# Headers are built as quoted strings and passed quoted. Splitting a
# header on whitespace is how a token containing a space becomes two
# curl arguments and a confusing failure at the peer.
auth_header() {
  tok=$(sync_token)
  if [ -n "$tok" ]; then printf 'X-Sync-Token: %s' "$tok"; fi
}

cmd_init() {
  # The remote is optional. The offline route - bundles over adb - has
  # no URL at all, and demanding one made the command that exists to
  # serve the no-internet case impossible to use for it.
  remote="${1:-}"
  mkdir -p "$SYNC_DIR" || die "cannot create $SYNC_DIR"
  [ -d "$SYNC_DIR/.git" ] || git -C "$SYNC_DIR" init -q -b main
  if [ -n "$remote" ]; then
    git -C "$SYNC_DIR" remote add origin "$remote" 2>/dev/null ||
      git -C "$SYNC_DIR" remote set-url origin "$remote"
  fi
  git_sane
  echo "  sync dir : $SYNC_DIR"
  echo "  remote   : ${remote:-(none - offline via bundle)}"
  [ -n "$remote" ] || echo "  add one later with:  pc-sync.sh init <remote-url>"
}

cmd_push() {
  api_up
  mkdir -p "$SYNC_DIR" || die "cannot create $SYNC_DIR"
  git_sane
  echo "[sync] exporting from the app (device: $DEVICE)"
  curl -s -m 300 -X POST "$API/api/sync/export" \
    -H 'Content-Type: application/json' \
    -d "{\"device\":\"$DEVICE\"}" |
    grep -o '"counts":{[^}]*}' || echo "  (no counts in response - check the app log)"

  git -C "$SYNC_DIR" add -A
  if ! git_changes; then
    echo "[sync] nothing to send"
    return 0
  fi
  git -C "$SYNC_DIR" commit -q -m "sync: $DEVICE $(date -u +%Y-%m-%dT%H:%M:%SZ)" &&
    echo "[sync] committed"

  # An empty remote cannot be pushed to without saying so; `git push`
  # would otherwise fail with a message about an unrelated refspec.
  if [ -z "$(git -C "$SYNC_DIR" remote get-url origin 2>/dev/null)" ]; then
    echo "[sync] no remote configured - committed locally only."
    echo "       Add one with:  pc-sync.sh init <remote-url>"
    return 0
  fi
  if git -C "$SYNC_DIR" push -q -u origin main 2>/dev/null; then
    echo "[sync] pushed"
  else
    echo "[sync] PUSH FAILED. Most likely another device pushed first."
    echo "       Resolve with:  git -C '$SYNC_DIR' pull --rebase"
  fi
}

cmd_pull() {
  apply=false
  [ "${1:-}" = "--apply" ] && apply=true
  # The git half of a pull needs no app, and the app half needs git to
  # have succeeded - so the app check used to come first, and a stopped
  # app hid a broken remote behind a message about starting things. The
  # API is checked only when the import is about to run, not before the
  # fetch.
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: pc-sync.sh init <url>"
  git_sane

  echo "[sync] pulling"
  if [ -n "$(git -C "$SYNC_DIR" remote get-url origin 2>/dev/null)" ]; then
    # Same as on the phone: a failed pull is not necessarily a
    # conflict, and swallowing stderr made every one look like one.
    pull_err=$(git -C "$SYNC_DIR" pull --ff-only origin main 2>&1 >/dev/null) || {
      echo "[sync] pull failed: $pull_err" | head -3
      if echo "$pull_err" | grep -qiE "could not resolve|connection|timed out|refused|authentication|credential|permission"; then
        echo "       This is the network or the credential, not a conflict."
        return 1
      fi
      # Unrelated histories are not a conflict either: they share no
      # commits at all, usually because one side started fresh. An empty
      # merge-base says so, and the remedy (adopt one side, then publish)
      # is different from resolving an edit collision.
      if ! git -C "$SYNC_DIR" merge-base HEAD FETCH_HEAD >/dev/null 2>&1; then
        echo "[sync] unrelated histories: this repo and the remote share no commits."
        echo "       Nothing was changed locally. To adopt the remote and publish"
        echo "       this device's state on top of it:"
        echo "         git -C '$SYNC_DIR' reset --hard FETCH_HEAD"
        echo "       then push. To keep this side instead, push with:"
        echo "         git -C '$SYNC_DIR' push origin main --force-with-lease"
        return 1
      fi
      echo "[sync] pull was not a fast-forward, so it was NOT applied."
      echo "       Someone edited the same record. Nothing was changed locally."
      return 1
    }
  else
    echo "       (no remote - using whatever fetch-bundle last brought in)"
  fi

  show_import() {
    curl -s -m 300 -X POST "$API/api/sync/import" \
      -H 'Content-Type: application/json' -d "{\"dry_run\":$1}" |
      tr ',' '\n' | grep -E 'created|updated|stale|revlog|awaiting' | sed 's/^/       /'
  }

  # Here, not earlier: git has already succeeded or failed by now,
  # so the "is the app up" question cannot mask a broken remote.
  api_up

  echo "[sync] import dry run (nothing written yet)"
  show_import true
  if [ "$apply" != "true" ]; then
    echo
    echo "  Dry run only. Re-run with --apply to write:"
    echo "       sh scripts/pc-sync.sh pull --apply"
    return 0
  fi
  echo "[sync] importing"
  show_import false
  echo "[sync] done"
}

# The transfer the network cannot do for us. A bundle is a git pack with
# the history the peer is missing, carried as one file over adb. No
# remote, no internet, no listening socket.
cmd_bundle() {
  path="${1:-}"
  [ -n "$path" ] || die "usage: pc-sync.sh bundle <path>"
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR"
  git -C "$SYNC_DIR" bundle create "$path" main ||
    die "cannot write bundle to $path"
  echo "  wrote $(du -h "$path" | cut -f1) to $path"
  echo "  carry it with:  adb push $path /data/local/tmp/sync.bundle"
}

cmd_fetch_bundle() {
  path="${1:-}"
  [ -n "$path" ] || die "usage: pc-sync.sh fetch-bundle <path>"
  [ -f "$path" ] || die "no bundle at $path"
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: pc-sync.sh init <url>"

  echo "[sync] fetching from $path"
  git -C "$SYNC_DIR" fetch -q "$path" main || die "cannot read the bundle"
  if git -C "$SYNC_DIR" merge -q --ff-only FETCH_HEAD 2>/dev/null; then
    echo "[sync] fast-forwarded to $(git -C "$SYNC_DIR" rev-parse --short HEAD)"
    return 0
  fi
  # Refusing rather than resetting. A history rewrite shipped in a
  # bundle once already left a phone unable to fast-forward, and the
  # guard that caught it then made the operator do the diagnosis by
  # hand. Say what to look at and what the safe command is.
  if git -C "$SYNC_DIR" log -q --oneline FETCH_HEAD..HEAD 2>/dev/null | grep -q .; then
    echo "[sync] this device has commits the bundle does not:"
    git -C "$SYNC_DIR" log --oneline FETCH_HEAD..HEAD | sed 's/^/         /'
    echo "       Inspect with:  git -C '$SYNC_DIR' log --oneline --graph -5"
    echo "       If they are stale, reset with:"
    echo "         git -C '$SYNC_DIR' reset --hard FETCH_HEAD"
    return 1
  fi
  echo "[sync] nothing to do - already current"
}

cmd_blobs() {
  peer="${1:-}"; action="${2:-}"
  [ -n "$peer" ] || die "usage: pc-sync.sh blobs <peer-url> pull|push"
  api_up
  hdr=$(auth_header)
  if [ -z "$hdr" ]; then
    echo "  note: no sync token found in $TOKEN_FILE - the peer will refuse if it requires one"
  fi

  # A real upload, not an offer. The offer endpoint only tells the peer
  # what we hold, which is useless when the peer cannot open a
  # connection back: on a LAN the direction that works is PC to phone.
  case "$action" in
    push)
      echo "[blobs] pushing to $peer"
      # shellcheck disable=SC2086
      curl -s -m 900 -X POST "$API/api/sync/blobs/push" ${hdr:+-H "$hdr"} \
        -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
        tr ',' '\n' | grep -E 'local|sent|failed' | sed 's/^/       /'
      ;;
    pull)
      echo "[blobs] pulling from $peer"
      # shellcheck disable=SC2086
      curl -s -m 900 -X POST "$API/api/sync/blobs/pull" ${hdr:+-H "$hdr"} \
        -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
        tr ',' '\n' | grep -E 'requested|fetched|failed' | sed 's/^/       /'
      ;;
    *) die "action must be pull or push" ;;
  esac
}

cmd_status() {
  api_up
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR"
  echo "  device  : $DEVICE"
  echo "  dir     : $SYNC_DIR"
  echo "  remote  : $(git -C "$SYNC_DIR" remote get-url origin 2>/dev/null || echo '(none)')"
  echo "  head    : $(git -C "$SYNC_DIR" log --oneline -1 2>/dev/null || echo '(no commits)')"
  git -C "$SYNC_DIR" status --porcelain 2>/dev/null | head -10 | sed 's/^/  changed /'
  echo "  exported manifest:"
  if [ -f "$SYNC_DIR/manifest.json" ]; then
    grep -o '"counts":{[^}]*}' "$SYNC_DIR/manifest.json" | sed 's/^/       /'
    grep -o '"[a-z_]*awaiting_blobs":[0-9]*' "$SYNC_DIR/manifest.json" | sed 's/^/       /'
  else
    echo "       (nothing exported yet)"
  fi
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  push) shift; cmd_push "$@" ;;
  pull) shift; cmd_pull "$@" ;;
  cycle)
    cmd_pull --apply && cmd_push
    ;;
  blobs) shift; cmd_blobs "$@" ;;
  bundle) shift; cmd_bundle "$@" ;;
  fetch-bundle) shift; cmd_fetch_bundle "$@" ;;
  status) shift; cmd_status "$@" ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
