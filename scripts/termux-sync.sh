#!/data/data/com.termux/files/usr/bin/sh
# Content sync between this phone and another Open Notebook install.
# Run in PLAIN Termux (needs git + network, not the guest).
#
#   sh ~/bin/onb-sync init <remote-url>     one-time: make the repo
#   sh ~/bin/onb-sync push                  export -> commit -> push
#   sh ~/bin/onb-sync pull                  pull  -> import
#   sh ~/bin/onb-sync pull --apply          import for real (default: dry run)
#   sh ~/bin/onb-sync cycle                 pull --apply, then push
#   sh ~/bin/onb-sync blobs <peer-url> pull   fetch missing file bytes
#   sh ~/bin/onb-sync blobs <peer-url> push   offer ours to a peer
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
  # Optional, because the offline route has no URL. Requiring one made
  # the command that exists to serve the no-internet case unusable for
  # the no-internet case.
  #
  # With a remote, create it first, e.g.:
  #   gh repo create on-notebook-sync --private --description 'Open Notebook content sync'
  # then re-run with the URL it prints.
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
  # The git half of a pull needs no app, and the app half needs git to
  # have succeeded - so the app check used to come first, and a stopped
  # app hid a broken remote behind a message about starting things. The
  # API is checked only when the import is about to run, not before the
  # fetch.
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: onb-sync init <url>"
  git_sane

  echo "[sync] pulling"
  # Stderr is captured, not discarded, and for a reason. This used to
  # swallow it and report every failure - including a network error or
  # a GitHub credential prompt answered wrong - as "someone edited the
  # same record". The operator then resolved a conflict that did not
  # exist, on a branch that had never been committed to. A pull failure
  # and a divergence are different problems with different remedies, so
  # the output has to say which one it is.
  pull_err=$(git -C "$SYNC_DIR" pull --ff-only origin main 2>&1 >/dev/null) || {
    echo "[sync] pull failed: $pull_err" | head -3
    if echo "$pull_err" | grep -qiE "could not resolve|connection|timed out|refused|authentication|credential|permission"; then
      echo "       This is the network or the credential, not a conflict."
      echo "       Check the connection and that git can reach GitHub:"
      echo "         git -C '$SYNC_DIR' ls-remote origin"
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
    echo "       Someone edited the same record. Inspect:"
    echo "         git -C '$SYNC_DIR' log --oneline -5"
    echo "         git -C '$SYNC_DIR' diff"
    echo "       Then resolve and retry. Nothing was changed locally."
    return 1
  }

  # Here, not earlier: git has already succeeded or failed by now,
  # so the "is the app up" question cannot mask a broken remote.
  api_up

  echo "[sync] import dry run (nothing written yet)"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    -H 'Content-Type: application/json' -d '{"dry_run":true}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog|queued|retry' | sed 's/^/       /'

  if [ "$apply" != "true" ]; then
    echo
    echo "  Dry run only. Re-run with --apply to write:"
    echo "       sh ~/bin/onb-sync pull --apply"
    return 0
  fi
  echo "[sync] importing"
  curl -s -m 300 -X POST "$API/api/sync/import" \
    -H 'Content-Type: application/json' -d '{"dry_run":false}' |
    tr ',' '\n' | grep -E 'created|updated|stale|revlog|queued|retry' | sed 's/^/       /'
  echo "[sync] done"
}

# Transfer the actual file bytes. The manifest moves hashes; this moves
# the PDFs behind them. Separate because they carry different risk and
# fail independently - a source can be imported and simply not openable
# until its bytes arrive, and that has to be a visible state.
#
#   sh ~/bin/onb-sync blobs <peer-url> pull   fetch what we are missing
#   sh ~/bin/onb-sync blobs <peer-url> push   tell the peer what we hold
#
# Direction depends on the network: this phone cannot open a connection
# to the PC (measured), so on a LAN the PC pulls *from* the phone. Both
# verbs exist anyway - over USB or a cloud relay the other way works.
cmd_blobs() {
  peer="${1:-}"; action="${2:-}"
  [ -n "$peer" ] || die "usage: $0 blobs <peer-url> pull|push"
  api_up

  # POSIX, not bash. This runs under Termux's sh, which rejects array
  # assignment outright - `auth=()` is a hard syntax error there and it
  # takes the whole script down rather than one branch.
  #
  # Headers are passed as quoted strings rather than through an
  # unquoted variable: splitting a header on whitespace is exactly how a
  # token containing a space would turn into two curl arguments and a
  # confusing failure at the peer.
  if [ -n "${SYNC_TOKEN:-}" ]; then
    tok_hdr="X-Sync-Token: $SYNC_TOKEN"
  else
    tok_hdr=""
    echo "  note: SYNC_TOKEN unset - the peer will refuse if it requires one"
  fi

  case "$action" in
    pull)
      echo "[blobs] asking $peer what we are missing"
      if [ -n "$tok_hdr" ]; then
        curl -s -m 120 -H "$tok_hdr" "$peer/api/sync/blobs" \
          > "$TMPDIR/blobs-hold.json" 2>/dev/null || die "cannot reach $peer"
      else
        curl -s -m 120 "$peer/api/sync/blobs" \
          > "$TMPDIR/blobs-hold.json" 2>/dev/null || die "cannot reach $peer"
      fi
      echo "[blobs] downloading"
      if [ -n "$tok_hdr" ]; then
        curl -s -m 300 -X POST "$API/api/sync/blobs/pull" -H "$tok_hdr" \
          -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
          tr ',' '\n' | grep -E 'requested|fetched|failed' | sed 's/^/       /'
      else
        curl -s -m 300 -X POST "$API/api/sync/blobs/pull" \
          -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
          tr ',' '\n' | grep -E 'requested|fetched|failed' | sed 's/^/       /'
      fi
      ;;
    push)
      # A real upload, not an offer. The offer endpoint only tells the
      # peer what we hold, which is useless when the peer cannot open a
      # connection back - the direction this network needs is PC to
      # phone, so the bytes have to go.
      echo "[blobs] pushing to $peer"
      if [ -n "$tok_hdr" ]; then
        curl -s -m 900 -X POST "$API/api/sync/blobs/push" -H "$tok_hdr" \
          -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
          tr ',' '\n' | grep -E 'local|sent|failed' | sed 's/^/       /'
      else
        curl -s -m 900 -X POST "$API/api/sync/blobs/push" \
          -H 'Content-Type: application/json' -d "{\"peer\":\"$peer\"}" |
          tr ',' '\n' | grep -E 'local|sent|failed' | sed 's/^/       /'
      fi
      ;;
    *)
      die "usage: $0 blobs <peer-url> pull|push"
      ;;
  esac
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

# --- offline route ------------------------------------------------------
#
# A git bundle carried over adb. Needs no internet and no listening
# socket at either end, which is the point: the phone cannot open a
# connection to the PC on the LAN, so "just fetch it" is not on offer
# without this.
#
# Blobs are not in here. A bundle carries the manifest and its hashes;
# the bytes still need the tunnel. That split is deliberate - a manifest
# can arrive hours before its bytes, and the import reports that as
# pending rather than as a failure.

cmd_bundle() {
  path="${1:-}"
  [ -n "$path" ] || die "usage: onb-sync bundle <path>"
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: onb-sync init <url>"
  mkdir -p "$(dirname "$path")" 2>/dev/null
  git -C "$SYNC_DIR" bundle create "$path" main || die "cannot write $path"
  echo "  wrote $(du -h "$path" | cut -f1) to $path"
  echo "  carry it with:  adb pull $path /data/local/tmp/sync.bundle"
}

cmd_fetch_bundle() {
  path="${1:-}"
  [ -n "$path" ] || die "usage: onb-sync fetch-bundle <path>"
  [ -f "$path" ] || die "no bundle at $path
         Copy it into the guest first, e.g.
           cp /data/local/tmp/sync.bundle $PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs/tmp/
         then re-run with that path."
  [ -d "$SYNC_DIR/.git" ] || die "no git repo at $SYNC_DIR - run: onb-sync init <url>"
  git_sane

  echo "[sync] fetching from $path"
  git -C "$SYNC_DIR" fetch -q "$path" main || die "cannot read the bundle"
  if git -C "$SYNC_DIR" merge -q --ff-only FETCH_HEAD 2>/dev/null; then
    echo "[sync] fast-forwarded to $(git -C "$SYNC_DIR" rev-parse --short HEAD)"
    return 0
  fi
  # Refuse rather than reset, and name the exact command. There is no
  # interactive git here, so "inspect and decide" has to be spelled out
  # rather than left as an instruction to go and look.
  if git -C "$SYNC_DIR" log -q --oneline FETCH_HEAD..HEAD 2>/dev/null | grep -q .; then
    echo "[sync] this device has commits the bundle does not:"
    git -C "$SYNC_DIR" log --oneline FETCH_HEAD..HEAD | sed 's/^/         /'
    echo "       If those are stale (e.g. rewritten history), reset with:"
    echo "         proot-distro login ubuntu -- git -C $SYNC_DIR reset --hard FETCH_HEAD"
    echo "       Inspect with (git lives in the guest, so it needs proot too):"
    echo "         proot-distro login ubuntu -- git -C $SYNC_DIR log --oneline --graph -5"
    echo "       If they are real edits, do NOT reset - rebase instead:"
    echo "         git -C '$SYNC_DIR' pull --rebase $path main"
    return 1
  fi
  echo "[sync] nothing to do - already current"
}

case "${1:-status}" in
  init) shift; cmd_init "${1:-}" ;;
  push) cmd_push ;;
  pull) shift; cmd_pull "${1:-}" ;;
  cycle)
    cmd_pull --apply && cmd_push
    ;;
  blobs) shift; cmd_blobs "${1:-}" "${2:-}" ;;
  bundle) shift; cmd_bundle "${1:-}" ;;
  fetch-bundle) shift; cmd_fetch_bundle "${1:-}" ;;
  status) cmd_status ;;
  *)
    echo "usage: $0 init <remote-url> | push | pull [--apply] | cycle | blobs <url> pull|push"
    echo "          | bundle <path> | fetch-bundle <path> | status"
    exit 1
    ;;
esac