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
  # Write beside the target and rename over it, never `cp` into place.
  #
  # `cp "$src" "$dst"` truncates and rewrites the *same inode*. A shell reads a
  # script incrementally through an open fd, and resumes at its current byte
  # offset after every command — so a script that overwrites itself mid-run finds
  # the file's contents shifted underneath it and carries on parsing whatever now
  # sits at that offset. That is what produced
  #
  #     onb-update: 518: Syntax error: ";;" unexpected (expecting "fi")
  #
  # from a file that dash parses cleanly: `onb-update` had installed a newer
  # version over itself while that newer version's older sibling was still
  # executing.
  #
  # `mv` replaces the directory entry instead. The running process keeps its open
  # fd on the old inode and is untouched, and a reader never sees a partial file.
  tmp="$dst.new.$$"
  if cp "$src" "$tmp" && chmod +x "$tmp" && mv "$tmp" "$dst"; then
    echo "  installed $2  <- $1"
  else
    rm -f "$tmp"
    echo "  FAILED  $2  <- $1"
    return 1
  fi
}

# A second copy of the launcher lives at /root/termux-stack.sh inside the
# guest, and for a long time nothing refreshed it. Every fix to
# termux-stack.sh reached ~/bin/onb-stack while the file `onb` actually
# executes kept its old contents - which is how the loopback binding was
# "deployed" and the API stayed reachable from the LAN.
#
# termux-launch.sh now calls the repo copy directly, so there is only one
# source. This refresh is belt and braces for anyone whose muscle memory
# reaches for the old path, who would otherwise keep invoking a file frozen
# at setup time.
refresh_guest_launcher() {
  guest_target="/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs/root/termux-stack.sh"
  [ -d "$(dirname "$guest_target")" ] || return 0
  if cmp -s "$REPO/termux-stack.sh" "$guest_target" 2>/dev/null; then
    echo "  guest /root/termux-stack.sh already current"
    return 0
  fi
  cp "$REPO/termux-stack.sh" "$guest_target" \
    && chmod +x "$guest_target" \
    && echo "  refreshed /root/termux-stack.sh in the guest"
}

echo "installing into $BIN"
rc=0
install_one termux-launch.sh onb       || rc=1
install_one termux-stack.sh  onb-stack || rc=1
install_one termux-model.sh  onb-model || rc=1
install_one termux-embed.sh  onb-embed || rc=1
# onb-sync was missing here, which is why the wrapper drifted out of
# step with termux-sync.sh and never picked up the `blobs` command.
install_one termux-sync.sh   onb-sync  || rc=1
# onb-update: fetch, merge --ff-only, reinstall the wrappers. Refuses to
# run while the stack answers, because updating underneath a live server
# leaves a half-old process tree. --restart does stop/update/start.
install_one termux-update.sh onb-update || rc=1
# onb-config: set a guest config value without hand-typing a proot copy
# across the Android /data boundary. It refuses an empty value, because a
# `KEY=` that looks configured and is not has already cost one debugging
# session - and it reports lengths, never values.
install_one termux-config.sh onb-config || rc=1
# onb-residency: the residency supervisor, as `onb-residency` rather than
# `termux-supervisor.py`. Renamed on install for the same reason as the rest -
# every other wrapper drops its `termux-` prefix - so that what an operator
# types matches what they read in the repo, and `install_one` stays the only
# place the mapping is written down.
install_one termux-supervisor.py onb-residency || rc=1
# onb-cpu is *sourced*, not run, and is executable only because install_one
# chmods everything it copies. The launcher and the embedder both read their
# thread budgets from it, so it has to land in the same deploy as the scripts
# that consume it - a missing one silently falls back to the floor's values.
install_one termux-cpu.sh     onb-cpu      || rc=1
# onb-stt / onb-tts: whisper.cpp and piper, started the same residency-managed
# way as the chat and embed servers. Installed here so a device that has the
# binaries can be brought up by the supervisor without hand-copying scripts -
# a wrapper that lives only in the repo is a wrapper the phone cannot run, which
# is the drift this whole file exists to prevent.
install_one termux-stt.sh     onb-stt      || rc=1
install_one termux-tts.sh     onb-tts      || rc=1
# onb-tts-server is the HTTP layer piper does not provide on this device: its
# own server.py binds 0.0.0.0 and hardcodes a Docker layout (/app/models,
# port 9600, Flask). On a phone that is both an unauthenticated network
# endpoint and a filesystem arrangement that does not exist, so the server is
# ours and depends only on the standard library.
install_one termux-tts-server.py onb-tts-server || rc=1
# onb-tts-ja-install: the piper-plus guest backend (:8092). Delegates into
# the proot guest, which is the only place onnxruntime's aarch64 wheel can
# install (glibc, not bionic) - so it sits beside onb-tts-server as the
# other half of the two-runtime split the forwarding rule implements.
install_one termux-tts-ja-install.sh onb-tts-ja-install || rc=1
# onb-build-piper: Termux packages no piper, no espeak-ng and no
# piper-phonemize, so the only route is building it. Not a wrapper the stack
# runs - it is here so the file the operator needs is in ~/bin with the rest,
# rather than in a repository they have to go and find.
install_one termux-build-piper.sh onb-build-piper || rc=1
  # onb-fetch-models: the multilingual whisper tiers and the zh/ja piper voices,
  # fetched by exact byte count rather than by `curl -C -`. Installed here for
  # the same reason as every other entry — a script that lives only in the repo
  # is a script the phone cannot run, which is the drift this file exists to
  # prevent. The first attempt at this ran the file straight out of the repo path
  # and failed with "No such file or directory" purely because nobody had
  # remembered to add it here.
  install_one termux-fetch-models.sh onb-fetch-models || rc=1
  # onb-speech-suite: the multilingual robustness suite. Stdlib Python only — a
  # device being measured must not acquire a dependency as part of being
  # measured — and it ships with the other wrappers so the phone can run it
  # against the two model servers residency manages.
  install_one termux-speech-suite.py onb-speech-suite || rc=1
  # onb-stt-benchmark: measures STT throughput across model tiers and thread
  # counts, so the shipped default is chosen from RTF on this device rather than
  # from arithmetic about it.
  install_one termux-stt-benchmark.sh onb-stt-benchmark || rc=1
refresh_guest_launcher || true

echo
if [ "$rc" -eq 0 ]; then
  echo "all installed. start with:  sh ~/bin/onb"
else
  echo "some wrappers were missing from the repo - see above"
fi
exit "$rc"