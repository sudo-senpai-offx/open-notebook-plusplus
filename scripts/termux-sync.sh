#!/data/data/com.termux/files/usr/bin/sh
# Content sync between this phone and another Open Notebook install.
# Run in PLAIN Termux (needs git + network, not the guest).
#
#   sh ~/bin/onb-sync init <remote-url>     one-time: make the repo
#   sh ~/bin/onb-sync push                  export -> commit -> push
#   sh ~/bin/onb-sync pull                  pull  -> import
#   sh ~/bin/onb-sync pull --apply          import for real (default: dry run)
#   sh ~/bin/onb-sync cycle                 pull --apply, then push
#   sh ~/bin/onb-sync status
#
# Why the app is a black box here: this script never reads the database.
# It drives the same three endpoints a human would, so what gets synced is
# whatever the API decided to export - not a parallel, diverging notion of
# "the data". The only thing living outside the app is the git repository.
#
# Why the sync dir points into the proot rootfs: the API writes it from
# inside the guest, and git reads it from Termux. It is one directory of
# plain files either side, so neither side needs to know the other exists.

set -u

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
API="${SYNC_API:-http://127.0.0.1:5055}"
SYNC_DIR="${SYNC_DIR:-$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs/root/app/open-notebook-plusplus/sync}"
DEVICE="${SYNC_DEVICE:-phone}"

die() { echo "  ERROR: $*" >&2; exit 1; }

api_up() {
  # `$(... || echo 000)` prints curl's own 000 *and* the fallback, giving
  # 000000. Assign then override instead.
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/health" 2>/dev/null) || code=000
  [ "$code" = "200" ] || die "the app is not answering on $API (HTTP $code).
         Start it first:  sh ~/bin/onb"
}

# git needs an identity even for a single-file repo, and the operator's
# global config is not guaranteed on a fresh Termux.
git_sane() {
  git -C "$SYNC_DIR" config user.email >/dev/null 2>&1 ||
    git -C "$SYNC_DIR" config user.email "sync@localhost"
  git -C "$SYNC_DIR" config user.name >/dev/null 2>&1 ||
    git -C "$SYNC_DIR" config user.name "open-notebook-sync"
}

git_changes() {
  [ -n "$(git -C "$SYNC_DIR" status --porcelain 2>/dev/null)" ]
}

cmd_init() {
  remote="${1:-}"
  [ -n "$remote" ] || die "usage: onb-sync init <remote-url>
         Create the repo first, e.g.:
           gh repo create on-notebook-sync --private --description 'Open Notebook content sync'
         then re-run with the URL it prints."
  mkdir -p "$SYNC_DIR" || die "cannot create $SYNC_DIR"
  [ -d "$SYNC_DIR/.git" ] || git -C "$SYNC_DIR" init -q -b main
  git -C "$SYNC_DIR" remote add origin "$remote" 2>/dev/null ||
    git -C "$SYNC_DIR" remote set-url origin "$remote"
  git_sane
  echo "  sync dir : $SYNC_DIR"
  echo "  remote   : $remote"
  echo
  echo "  The app writes into that directory; it must see the same path."
  echo "  If it was created after the app started, restart it:"
  echo "    Ctrl+C on ~/bin/onb, then sh ~/bin/onb"
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
  if git -C "$SYNC_DIR" push -q origin main 2>/dev/null; then
    echo "[sync] pushed"
  else
    echo "[sync] PUSH FAILED. Most likely another device pushed first."
    echo "       Resolve with:  git -C '$SYNC_DIR' pull --rebase"
  fi
}

cmd_pull() {
  apply=false
  [ "${1:-}" = "--apply" ] && apply=true
  api_up
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: onb-sync init <url>"
  git_sane

  echo "[sync] pulling"
  if ! git -C "$SYNC_DIR" pull -q --ff-only origin main 2>/dev/null; then
    echo "[sync] pull was not a fast-forward, so it was NOT applied."
    echo "       Someone edited the same record. Inspect:"
    echo "         git -C '$SYNC_DIR' log --oneline -5"
    echo "         git -C '$SYNC_DIR' diff"
    echo "       Then resolve and retry. Nothing was changed locally."
    return 1
  fi

  echo "[sync] import dry run (nothing written yet)"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    -H 'Content-Type: application/json' -d '{"dry_run":true}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog' | sed 's/^/       /'

  if [ "$apply" != "true" ]; then
    echo
    echo "  Dry run only. Re-run with --apply to write:"
    echo "       sh ~/bin/onb-sync pull --apply"
    return 0
  fi
  echo "[sync] importing"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    -H 'Content-Type: application/json' -d '{"dry_run":false}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog' | sed 's/^/       /'
  echo "[sync] done"
}

cmd_status() {
  echo "  sync dir : $SYNC_DIR"
  echo "  api      : $API"
  echo "  device   : $DEVICE"
  if [ -d "$SYNC_DIR/.git" ]; then
    echo "  remote   : $(git -C "$SYNC_DIR" remote get-url origin 2>/dev/null || echo none)"
    echo "  last     : $(git -C "$SYNC_DIR" log -1 --format='%h %s' 2>/dev/null || echo none)"
    git_changes && echo "  uncommitted changes: yes" || echo "  uncommitted changes: no"
  else
    echo "  git      : not initialised"
  fi
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$API/api/sync/status" 2>/dev/null) || code=000
  if [ "$code" = "200" ]; then
    echo "  app sees : $(curl -s -m 10 "$API/api/sync/status" | tr -d ' \n')"
  else
    echo "  app      : not answering (HTTP $code)"
  fi
}

case "${1:-status}" in
  init) shift; cmd_init "${1:-}" ;;
  push) cmd_push ;;
  pull) shift; cmd_pull "${1:-}" ;;
  cycle)
    cmd_pull --apply && cmd_push
    ;;
  status) cmd_status ;;
  *)
    echo "usage: $0 init <remote-url> | push | pull [--apply] | cycle | status"
    exit 1
    ;;
esac