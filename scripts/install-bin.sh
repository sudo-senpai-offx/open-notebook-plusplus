#!/data/data/com.termux/files/usr/bin/sh
# Install the Termux-side wrappers into ~/bin — run in PLAIN Termux.
#
#   sh install-bin.sh [path/to/repo/scripts]
#
# Why this exists: onb, onb-stack, onb-model and onb-embed are COPIES in
# ~/bin, not the files in the repo. Adding scripts/termux-embed.sh to the
# repo therefore did nothing on the device — `sh ~/bin/onb-embed` failed
# with "No such file or directory" while the script sat committed in git.
# That drift is the bug, not the copy.
#
# The repo lives inside the proot guest, so the default path below reaches
# into the guest rootfs from Termux. Override with an argument if that
# layout differs.
#
# Deliberately not run from inside proot: ~/bin lives in Termux's home,
# outside the guest, so a guest-side install is not guaranteed to be able to
# write it.

set -u

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
DEFAULT_REPO="$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs/root/app/open-notebook-plusplus/scripts"
REPO="${1:-$DEFAULT_REPO}"
BIN="$HOME/bin"

if [ ! -d "$REPO" ]; then
  echo "ERROR: scripts directory not found: $REPO"
  echo
  echo "Pass the path explicitly:"
  echo "  sh install-bin.sh /path/to/open-notebook-plusplus/scripts"
  echo
  echo "Find it with:"
  echo "  find $PREFIX/var/lib/proot-distro -name termux-embed.sh 2>/dev/null"
  exit 1
fi

mkdir -p "$BIN"

# Explicit mapping: the repo names are termux-*.sh, the installed names are
# onb*, which is why a plain copy of every file would be wrong.
install_one() {
  src="$REPO/$1"
  dst="$BIN/$2"
  if [ ! -f "$src" ]; then
    echo "  MISSING  $1"
    return 1
  fi
  cp "$src" "$dst" && chmod +x "$dst" && echo "  installed $2  <- $1"
}

echo "installing into $BIN"
rc=0
install_one termux-launch.sh onb       || rc=1
install_one termux-stack.sh  onb-stack || rc=1
install_one termux-model.sh  onb-model || rc=1
install_one termux-embed.sh  onb-embed || rc=1

echo
if [ "$rc" -eq 0 ]; then
  echo "all four installed. start with:  sh ~/bin/onb"
else
  echo "some wrappers were missing from the repo - see above"
fi
exit "$rc"