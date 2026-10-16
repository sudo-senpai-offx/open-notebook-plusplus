#!/data/data/com.termux/files/usr/bin/sh
# Set a configuration value inside the proot guest. Run in PLAIN Termux.
#
#   sh ~/bin/onb-config list                 # key names only, never values
#   sh ~/bin/onb-config set KEY --from FILE  # preferred: never transits a chat
#   sh ~/bin/onb-config check KEY            # length only, never the value
#
# Why this exists
# ---------------
# Setting a secret in the guest has been typed by hand four times now and
# failed three of them, in three distinct ways:
#
#   1. The `>>` redirect was evaluated by Termux's shell rather than the
#      guest's, because the command was not quoted - so it tried to write a
#      path that does not exist on the Android side.
#   2. The copy was run from inside the guest, where `/data/local/tmp` does
#      not exist - proot deliberately does not mount Android `/data`.
#   3. The staged file was chmod 600 owned by `shell`, so Termux could not
#      read a file only it could see.
#
# And the one that actually mattered: a `printf` from a `$(cat)` of a file
# that was not there wrote `KEY=` with an **empty value**. The key was
# present, grep found it, and the configuration looked correct while
# providing no credential at all. Auth would have silently stayed off on a
# self-hosted phone.
#
# So this refuses an empty value outright, reports the *length* rather than
# the value, and exits non-zero on any failure. A run that says it wrote
# something means it wrote something.
#
# The guest-side work is written to a file and executed, rather than passed
# as a `bash -lc` string. Nesting escaped quotes inside another shell's
# single-argument form is how quoting bugs survive review; here the guest
# logic is plain POSIX that can be read and checked on its own.

set -eu

ROOTFS="$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs"

say() { printf '%s\n' "$*"; }
die() {
  printf 'onb-config: %s\n' "$*" >&2
  exit 1
}

[ -d "$ROOTFS" ] || die "no proot rootfs at $ROOTFS - is proot-distro installed?"

# Guest-side helper, installed per invocation. Idempotent and short-lived.
GUEST_HELPER_IN_GUEST="/tmp/onb-config-guest.sh"
GUEST_HELPER_ON_HOST="$ROOTFS$GUEST_HELPER_IN_GUEST"

install_helper() {
  cat >"$GUEST_HELPER_ON_HOST" <<'HELPER'
#!/bin/sh
set -eu
ENV_FILE="/root/app/open-notebook-plusplus/.env"
action="$1"; key="${2:-}"

report_len() {
  # Echoes a length, never a value.
  n=${#1}
  if [ "$n" -eq 0 ]; then
    echo "  $key: PRESENT BUT EMPTY - that is not a configured value"
    exit 1
  fi
  echo "  $key: set, $n chars"
}

case "$action" in
  list)
    [ -f "$ENV_FILE" ] || { echo "  (no .env in the guest yet)"; exit 0; }
    grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | tr -d '=' | sort | sed 's/^/  /'
    ;;
  check)
    [ -f "$ENV_FILE" ] || { echo "  $key: no .env"; exit 1; }
    line=$(grep -m1 -E "^$key=" "$ENV_FILE" || true)
    if [ -z "$line" ]; then echo "  $key: absent"; exit 1; fi
    report_len "${line#*=}"
    ;;
  set)
    [ -f "$ENV_FILE" ] || { echo "  no .env in the guest"; exit 1; }
    value=$(cat /tmp/onb-config-value)
    # Replace any existing key, empty or not. Delete-then-append keeps the
    # file's ordering stable and avoids sed quoting games around values
    # that contain slashes.
    sed -i "/^$key=/d" "$ENV_FILE"
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
    chmod 600 "$ENV_FILE"
    report_len "$value"
    ;;
  *) echo "bad action: $action" >&2; exit 2 ;;
esac
rm -f /tmp/onb-config-value
HELPER
  chmod 700 "$GUEST_HELPER_ON_HOST"
}

cmd_set() {
  key="${1:-}"
  [ -n "$key" ] || die "usage: onb-config set KEY --from FILE"
  case "$key" in
    *[!A-Za-z0-9_]* | "") die "KEY must be a plain identifier, got '$key'" ;;
  esac

  [ "${2:-}" = "--from" ] || die "use: onb-config set KEY --from FILE (a literal goes through your shell history)"
  src="${3:-}"
  [ -n "$src" ] || die "--from needs a file path"
  [ -r "$src" ] || die "cannot read $src"

  stage="$ROOTFS/tmp/onb-config-value"
  # Command substitution strips trailing newlines, which is what we want:
  # `echo secret > f` and every editor add one, and a password ending in a
  # newline is essentially never intended. Written to a file, not passed as
  # an argument, so it does not sit in argv.
  printf '%s' "$(cat "$src")" >"$stage"
  chmod 600 "$stage"

  size=$(wc -c <"$stage" | tr -d ' ')
  # The assertion that would have caught the empty-`$(cat)` case. Everything
  # downstream assumes a real value, so this is checked, not assumed.
  [ "$size" -gt 0 ] || {
    rm -f "$stage"
    die "refusing to write an empty value for $key"
  }

  install_helper
  proot-distro login ubuntu -- "$GUEST_HELPER_IN_GUEST" set "$key"
  rm -f "$stage" "$GUEST_HELPER_ON_HOST"
  say "  (value never printed; verify with: onb-config check $key)"
}

case "${1:-}" in
  list) install_helper; proot-distro login ubuntu -- "$GUEST_HELPER_IN_GUEST" list; rm -f "$GUEST_HELPER_ON_HOST" ;;
  set) shift; cmd_set "$@" ;;
  check) shift; install_helper; proot-distro login ubuntu -- "$GUEST_HELPER_IN_GUEST" check "${1:-}"; rc=$?; rm -f "$GUEST_HELPER_ON_HOST"; exit $rc ;;
  *) say "usage: onb-config list | set KEY --from FILE | check KEY"; exit 2 ;;
esac