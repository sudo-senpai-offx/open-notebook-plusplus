#!/data/data/com.termux/files/usr/bin/sh
# Update open-notebook-plusplus in the proot guest. Run in PLAIN Termux.
#
#   sh ~/bin/onb-update            # update if the stack is stopped
#   sh ~/bin/onb-update --restart  # stop, update, start again
#   sh ~/bin/onb-update --check    # report only, change nothing
#
# Where the update comes from, in order of preference:
#
#   ONB_UPDATE_SRC   a path or URL. A .bundle file is fetched and merged
#                    with `git fetch <bundle> main`; a git remote is
#                    fetched and merged with `git pull --ff-only`.
#   $ONB_BUNDLE      convenience alias, same meaning.
#   default          /data/local/tmp/plusplus.bundle, which is where
#                    `adb push` puts it. Deliberately outside the guest
#                    rootfs: proot does not mount Android /data, so a
#                    bundle pushed to /data/local/tmp is unreachable from
#                    inside the guest until it is copied across.
#
# On refusing to run while the stack is up
# ----------------------------------------
# The default is to refuse, because updating underneath a running server
# is how you get a half-old, half-new process tree: Python has already
# imported the old modules, the worker pool is running old code, and the
# next restart behaves differently from the one you just watched. There
# is no safe way to do this from the outside.
#
# `--restart` is the better workaround, and is why the refusal is an
# error rather than a dead end: it stops the stack through the same path
# `onb stop` uses, updates, then starts again. One command, no window
# where you have to remember what to run next.
#
# Nothing to do is success
# ------------------------
# Already up to date exits 0 and prints one line. Only a real failure -
# a dirty working tree, a non-fast-forward, a missing bundle, the stack
# being up without --restart - is non-zero. A launcher that treats "no
# update" as an error teaches you to ignore the exit code.

set -u

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
ROOTFS="$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs"
REPO_IN_GUEST="/root/app/open-notebook-plusplus"
REPO="$ROOTFS$REPO_IN_GUEST"
BUNDLE_IN_GUEST="/tmp/plusplus.bundle"
DEFAULT_BUNDLE="/data/local/tmp/plusplus.bundle"

API_PORT="${ONB_API_PORT:-15055}"
HEALTH_URL="http://127.0.0.1:$API_PORT/api/sync/status"

STATE="${ONB_UPDATE_STATE:-$HOME/.onb-update-checked}"
MAX_AGE="${ONB_UPDATE_MAX_AGE:-86400}"   # re-check at most once a day

CHECK_ONLY=0
RESTART=0
QUIET=0

for arg in "$@"; do
  case "$arg" in
    --check)   CHECK_ONLY=1 ;;
    --restart) RESTART=1 ;;
    --quiet)   QUIET=1 ;;
    -h|--help)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

# --- is the stack up? -----------------------------------------------------
#
# The API answering is the honest signal. Looking for a process by name
# catches the wrong things: `llama-server` runs outside the guest and
# has no business blocking a code update, and the guest processes are
# named after their python modules rather than anything recognisable.
stack_is_up() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 3 -o /dev/null "$HEALTH_URL" 2>/dev/null && return 0
  fi
  # No curl: fall back to the guest's python, which is always present.
  if [ -x "$ROOTFS/usr/bin/python3" ] && [ -d "$REPO" ]; then
    proot-distro login ubuntu -- \
      /usr/bin/python3 -c \
      "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',$API_PORT))==0 else 1)" \
      >/dev/null 2>&1 && return 0
  fi
  return 1
}

stop_stack() {
  say "  stopping the stack (onb stop)"
  if [ -f "$HOME/bin/onb" ]; then
    sh "$HOME/bin/onb" stop >/dev/null 2>&1
  fi
  # Belt and braces: the launcher parks on `read`, so it only exits on a
  # signal or a closed stdin. Give it a moment, then escalate.
  n=0
  while [ "$n" -lt 15 ]; do
    stack_is_up || return 0
    sleep 1
    n=$((n + 1))
  done
  say "  the API is still answering; giving up"
  return 1
}

# --- what would we update to? ---------------------------------------------
#
# Prints the commit the repo would move to, or nothing if it cannot tell.
resolve_remote() {
  if [ -n "${ONB_UPDATE_SRC:-}" ]; then
    printf '%s' "$ONB_UPDATE_SRC"
  elif [ -n "${ONB_BUNDLE:-}" ]; then
    printf '%s' "$ONB_BUNDLE"
  else
    printf '%s' "$DEFAULT_BUNDLE"
  fi
}

in_guest() {
  proot-distro login ubuntu -- "$@"
}

current_commit() {
  in_guest git -C "$REPO_IN_GUEST" rev-parse HEAD 2>/dev/null
}

# Stage the bundle inside the guest, setting STAGED to the path it can be
# fetched from.
#
# Deliberately not `STAGED=$(stage_source ...)`. Command substitution
# runs the function in a subshell, so STAGE_ERROR set inside it never
# reaches the caller: the script reported "ERROR: " followed by nothing,
# which is worse than no message because it looks like the message was
# truncated rather than lost.
#
# Only needed for a file. A git remote is reachable from inside proot
# through the host's network, so a URL needs no copying - and /data is
# not mounted there anyway, so a pushed bundle would not be visible.
#
# Errors go into STAGE_ERROR rather than being printed here: this runs
# inside `$( )`, so anything it writes to stdout is captured as the
# return value and the message vanishes. That is not a cosmetic problem -
# the most likely failure, a bundle that was never pushed, would exit
# with no explanation at all.
STAGE_ERROR=""
STAGED=""

stage_source() {
  src="$1"
  STAGE_ERROR=""
  STAGED=""
  case "$src" in
    http://*|https://*|git@*|ssh://*)
      if ! in_guest git -C "$REPO_IN_GUEST" fetch "$src" main >/dev/null 2>&1; then
        STAGE_ERROR="could not fetch $src"
        return 1
      fi
      STAGED="FETCH_HEAD"
      return 0 ;;
    *)
      if [ ! -f "$src" ]; then
        STAGE_ERROR="no bundle at $src
  push one with:  adb push <bundle> /data/local/tmp/plusplus.bundle"
        return 1
      fi
      if ! cp "$src" "$ROOTFS$BUNDLE_IN_GUEST" 2>/dev/null; then
        STAGE_ERROR="could not copy the bundle into the guest at $BUNDLE_IN_GUEST"
        return 1
      fi
      # A bundle is not a ref, so it cannot be rev-parsed - it has to be
      # fetched like any other remote. This is the same step as the
      # manual procedure (`git fetch /tmp/plusplus.bundle main`), and
      # forgetting it is what made an earlier draft report "could not
      # resolve a commit" for a perfectly good bundle.
      if ! in_guest git -C "$REPO_IN_GUEST" fetch "$BUNDLE_IN_GUEST" main >/dev/null 2>&1; then
        STAGE_ERROR="git fetch rejected $src. Is it a bundle of this repo?"
        return 1
      fi
      STAGED="FETCH_HEAD"
      return 0 ;;
  esac
}

# Wraps stage_source and reports any failure on the real stdout.
resolve_staged() {
  stage_source "$(resolve_remote)" || {
    say "ERROR: $STAGE_ERROR"
    return 1
  }
  return 0
}

head_of() {
  in_guest git -C "$REPO_IN_GUEST" rev-parse "$1" 2>/dev/null
}

do_check() {
  resolve_staged || return 1
  staged="$STAGED"
  target="$(head_of "$staged")"
  current="$(current_commit)"

  if [ -z "$target" ]; then
    say "ERROR: could not resolve a commit from $src"
    return 1
  fi
  if [ "$target" = "$current" ]; then
    say "up to date ($(printf '%.8s' "$current"))"
    return 0
  fi

  # Only a fast-forward is safe. A divergent history means someone edited
  # the repo on the device, and silently resetting it would throw their
  # work away - which is exactly what a silent `git reset --hard` does.
  if in_guest git -C "$REPO_IN_GUEST" merge-base --is-ancestor "$current" "$staged" 2>/dev/null; then
    say "update available: $(printf '%.8s' "$current") -> $(printf '%.8s' "$target")"
    in_guest git -C "$REPO_IN_GUEST" log --oneline "$current..$staged" 2>/dev/null | sed 's/^/    /'
    return 10
  fi

  say "ERROR: the guest repo has diverged from $src"
  say "  $(printf '%.8s' "$current") is not an ancestor of $(printf '%.8s' "$target")."
  say "  Refusing to reset it: the guest may have local edits."
  say "  Inspect with:  proot-distro login ubuntu -- git -C $REPO_IN_GUEST log --oneline -5"
  return 2
}

do_apply() {
  resolve_staged || return 1

  say "  merging"
  staged="$STAGED"
  if ! in_guest git -C "$REPO_IN_GUEST" merge --ff-only "$staged" >/dev/null 2>&1; then
    say "ERROR: merge failed. The guest tree may be dirty:"
    say "  proot-distro login ubuntu -- git -C $REPO_IN_GUEST status --short"
    return 1
  fi

  # The wrappers in ~/bin are copies, so merging does not update them.
  # Skipping this is how onb-embed silently drifted from termux-embed.sh
  # in the first place.
  say "  reinstalling the Termux wrappers"
  if [ -f "$REPO/scripts/install-bin.sh" ]; then
    sh "$REPO/scripts/install-bin.sh" 2>&1 | sed 's/^/    /' || {
      say "ERROR: install-bin.sh failed"
      return 1
    }
  fi

  say "  updated to $(printf '%.8s' "$(current_commit)")"
  return 0
}

# --- launch hook ----------------------------------------------------------
#
# Runs from `onb` before it starts anything. Never updates: updating
# during a launch is the conflict this script exists to avoid, and it
# would also make start-up depend on the network. It only checks, and it
# only does so once a day, because a `git fetch` on every launch is real
# latency on a phone for information that rarely changes.
launch_check() {
  [ -d "$REPO" ] || return 0
  now="$(date +%s 2>/dev/null || echo 0)"
  if [ "$now" -gt 0 ] && [ -f "$STATE" ]; then
    last="$(cat "$STATE" 2>/dev/null || echo 0)"
    if [ "$now" -lt $((last + MAX_AGE)) ]; then
      return 0
    fi
  fi
  printf '%s' "$now" > "$STATE" 2>/dev/null || true

  # Only the exit code matters here; do_check's own output is discarded.
  do_check >/dev/null 2>&1
  case "$?" in
    10) printf '  update available: sh ~/bin/onb-update --restart\n' ;;
    *)  : ;;  # up to date, offline, no bundle, or divergent. All silent:
           # a launch hook must never fail, and never make the user
           # read a message on every start.
  esac
}

case "${1:-}" in
  --launch-check) launch_check; exit 0 ;;
esac

say "open-notebook-plusplus update"

# A check-only run is read-only, so it is allowed with the stack up - in
# fact that is the case worth checking from, since you want to know
# before you commit to a restart. Only a merge needs the quiet.
if [ "$CHECK_ONLY" -eq 0 ] && stack_is_up; then
  if [ "$RESTART" -eq 0 ]; then
    say "ERROR: the stack is running (API answering on $API_PORT)."
    say "  Updating underneath a live server leaves a half-old process tree."
    say
    say "  Either stop it first:   sh ~/bin/onb stop"
    say "  Or let this do it:      sh ~/bin/onb-update --restart"
    exit 1
  fi
fi

do_check
rc=$?
case "$rc" in
  0)
    if [ "$CHECK_ONLY" -eq 1 ]; then exit 0; fi
    exit 0 ;;
  10)
    if [ "$CHECK_ONLY" -eq 1 ]; then exit 0; fi
    ;;
  *) exit "$rc" ;;
esac

if stack_is_up && [ "$RESTART" -eq 1 ]; then
  stop_stack || exit 1
fi

do_apply || exit 1

if [ "$RESTART" -eq 1 ]; then
  say "  starting the stack again"
  if [ -f "$HOME/bin/onb" ]; then
    nohup sh "$HOME/bin/onb" >/dev/null 2>&1 &
  else
    say "  WARNING: ~/bin/onb not found; start the stack yourself"
  fi
fi

exit 0