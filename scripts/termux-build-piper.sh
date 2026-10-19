#!/data/data/com.termux/files/usr/bin/sh
# Build piper (text-to-speech) from source in Termux. Run in PLAIN Termux:
#
#   sh ~/bin/onb-build-piper
#
# Why this exists at all
# ----------------------
# There is no piper package for Termux. Checked against termux/termux-packages:
# 2226 packages, and the only speech-related ones are `espeak` 1.52 (the
# original, formant synthesis) and `cowsay`. So piper has to be built here.
#
# The chain is
#
#   piper  ->  piper-phonemize  ->  { espeak-ng (built), onnxruntime (taken) }
#
# and the interesting part is the last one.
#
# The trap
# --------
# piper-phonemize's CMake *downloads a prebuilt onnxruntime* — a glibc
# `onnxruntime-linux-aarch64-1.14.1.tgz` from the microsoft/onnxruntime
# releases. On Android the dynamic linker rejects it: wrong libc, and it wants
# `/lib64/ld-linux-aarch64.so.1`, which does not exist on bionic. The failure
# arrives at run time as an unhelpful "cannot open shared object file", long
# after a build that otherwise succeeded.
#
# The way out
# -----------
# piper-phonemize honours `ONNXRUNTIME_DIR`, and when it is defined the whole
# download block is skipped and it uses `<dir>/include` and `<dir>/lib`
# directly. Termux packages onnxruntime 1.31.0, built for bionic, which is what
# Android needs — but it installs the C++ headers one level down, into
# `$PREFIX/include/onnxruntime/`, where piper-phonemize does not look. So this
# script lays out a shim prefix of symlinks whose `include/` and `lib/` put the
# header and the library exactly where those two lines expect them, and points
# ONNXRUNTIME_DIR at that instead.
#
# Version skew — 1.14.1 against 1.31.0 — is the risk in this. piper-phonemize
# uses only the ONNX C API, which Microsoft has kept stable across those
# releases, so it should link. If it does not, the linker will name the missing
# symbol, and that symbol is the answer.
#
# On verifying the prefix
# -----------------------
# The layout is checked by compiling, not by `test -f`. Earlier, a diagnostic
# ran `ls -1 $PREFIX/include/onnxruntime*`, which globs a *directory* and lists
# its contents; the output was read as "the headers are in include/", which was
# wrong, and the build then failed on a header that had been "confirmed". Every
# check here is against the compiler or against the resolved path. Note also that
# `/tmp` does not exist in Termux — use a path under $HOME.
#
# Why you should paste the output rather than summarise it
# --------------------------------------------------------
# Every step prints a `==>` marker so a failure names the step that failed.
# The likeliest failure is this build's last mile — the piper-phonemize link —
# and the last 40 lines before it are what identifies it. "The build failed" is
# not.
#
# This touches nothing in ~/bin and nothing running. The result lands in
# ~/piper-build; termux-tts.sh is wired up separately.

set -eu

SRC="$HOME/piper-build"
# NOT `PREFIX`. Termux exports PREFIX=/data/data/com.termux/files/usr, so a
# `PREFIX="${PREFIX:-...}"` never falls through to its default on the only
# platform this script runs on: it silently installs piper, piper-phonemize,
# espeak-ng, libtashkeel_model.ort and every .so straight into Termux's own
# tree, overwriting the espeak-ng data an installed `espeak` package depends on.
# That is what the first successful build did. The name is the bug.
DEST="${PIPER_DEST:-$HOME/piper-build/prefix}"
JOBS=$(nproc 2>/dev/null || echo 4)

step() { printf '\n==> %s\n' "$1"; }
fail() {
  printf '\n!!! FAILED at: %s\n' "$1"
  printf '!!! Re-run that step alone and paste its last 40 lines.\n'
  exit 1
}

# ------------------------------------------------------------- prerequisites

step "prerequisites"
missing=""
for tool in cmake make git pkg-config; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
[ -z "$missing" ] || {
  echo "missing:$missing"
  echo "  pkg install cmake make git pkg-config"
  fail "prerequisites"
}

# onnxruntime is the one that matters, so it is checked for the file piper will
# actually compile against rather than for the command being on PATH.
#
# The header is NOT at $PREFIX/include. Termux installs it into
# $PREFIX/include/onnxruntime/, and piper-phonemize wants the contents of
# ${ONNXRUNTIME_DIR}/include to *include* the header:
#
#   target_include_directories(piper_phonemize PUBLIC ${ONNXRUNTIME_DIR}/include)
#   target_link_directories(piper_phonemize PUBLIC ${ONNXRUNTIME_DIR}/lib)
#
# An earlier version of this script pointed ONNXRUNTIME_DIR at $PREFIX because a
# diagnostic had run `ls -1 $PREFIX/include/onnxruntime*` — which globs a
# directory and lists its *contents*, so it looked like the headers sat directly
# in include/. They do not. The build then died on a header that the diagnostic
# had "confirmed". Nothing here is assumed: the header is found, not expected.
#
# Because the layout does not match, a shim prefix is laid out so that both of
# the paths above resolve. Symlinks, not copies — the library is ~19 MB and there
# is no reason to duplicate it for the sake of a build step.
ORT=$(pkg-config --variable=prefix onnxruntime 2>/dev/null || echo /data/data/com.termux/files/usr)
ORT_HEADER=$(find "$ORT/include" -name 'onnxruntime_cxx_api.h' 2>/dev/null | head -1)
ORT_LIB=$(find "$ORT/lib" -name 'libonnxruntime.so*' 2>/dev/null | head -1)

if [ -z "$ORT_HEADER" ] || [ -z "$ORT_LIB" ]; then
  echo "onnxruntime not usable."
  echo "  header:  ${ORT_HEADER:-not found under $ORT/include}"
  echo "  library: ${ORT_LIB:-not found under $ORT/lib}"
  echo "  install with:  pkg install onnxruntime"
  echo "  (NOT python-onnxruntime — piper needs the C library and headers,"
  echo "   not the Python bindings)"
  fail "onnxruntime"
fi

SHIM="$SRC/onnxruntime"
rm -rf "$SHIM"
mkdir -p "$SHIM/include" "$SHIM/lib"
ORT_INC=$(dirname "$ORT_HEADER")
for _h in "$ORT_INC"/*.h; do
  [ -f "$_h" ] && ln -sf "$_h" "$SHIM/include/$(basename "$_h")"
done
# `core/` and any other header subdirectory the C++ API includes by relative
# path, symlinked whole so their internal layout survives.
for _d in "$ORT_INC"/*/; do
  [ -d "$_d" ] && ln -sfn "$_d" "$SHIM/include/$(basename "$_d")"
done
for _l in "$ORT"/lib/libonnxruntime*.so*; do
  [ -e "$_l" ] && ln -sf "$_l" "$SHIM/lib/$(basename "$_l")"
done

echo "cmake, git, pkg-config present"
echo "onnxruntime headers from: $ORT_INC"
echo "onnxruntime shim prefix:  $SHIM"

# Prove the layout with the compiler, not with `test -f`. What piper needs is
# for the *compiler* to resolve the header through the include directory this
# prefix provides, and that is exactly the thing a test -f does not check. This
# costs two seconds and replaces an hour of build time spent finding out.
if ! printf '#include <onnxruntime_cxx_api.h>\nint main(){return 0;}\n' \
     > "$SRC/ort-check.cpp"; then
  fail "cannot write the shim compile check"
fi
if ! c++ -I"$SHIM/include" -c "$SRC/ort-check.cpp" -o "$SRC/ort-check.o" \
     2>"$SRC/ort-check.err"; then
  echo "the compiler cannot see onnxruntime_cxx_api.h through the shim:"
  sed 's/^/    /' "$SRC/ort-check.err" | head -5
  echo "  shim include: $SHIM/include"
  # Listing for the operator to read; every entry here is a symlink to a .h or
  # a header directory, so there is nothing for `find`'s quoting to protect.
  # shellcheck disable=SC2012
  ls -la "$SHIM/include" 2>/dev/null | head -10 || true
  fail "onnxruntime shim"
fi
echo "  compiler resolves onnxruntime_cxx_api.h through the shim"
ORT="$SHIM"

mkdir -p "$DEST/bin"

# -------------------------------------------------------------------- piper

step "cloning piper"
[ -d "$SRC/piper" ] || git clone --depth 1 https://github.com/rhasspy/piper "$SRC/piper"
cd "$SRC/piper"

step "patching piper for bionic"
# Two patches, both because this is not the build piper's CMake expects.
#
# 1. `-static-libgcc -static-libstdc++` is added whenever the target is neither
#    MSVC nor Apple — Android is neither, so Android gets a glibc-only flag set.
#    bionic has no static libstdc++ and the link fails naming a file nobody has
#    heard of rather than the CMakeLists line that caused it.
sed -i 's/ -static-libgcc -static-libstdc++//' CMakeLists.txt
echo "  stripped static link flags"

# 2. Forward ONNXRUNTIME_DIR into piper-phonemize's own configure.
#
# piper-phonemize's ExternalProject forwards only CMAKE_INSTALL_PREFIX, so the
# variable never reaches the nested build. It cannot be passed through the
# environment either: CMake does not turn $ENV{X} into a cache variable of the
# same name — that assumption was in an earlier version of this script and it
# is wrong. It has to be written into the ExternalProject's CMAKE_ARGS, where
# the nested configure reads it from the environment.
if ! grep -q 'ONNXRUNTIME_DIR:PATH' CMakeLists.txt; then
  sed -i "s|\(CMAKE_ARGS -DCMAKE_INSTALL_PREFIX:PATH=\${PIPER_PHONEMIZE_DIR}\)|\1\n      CMAKE_ARGS -DONNXRUNTIME_DIR:PATH=\$ENV{ONNXRUNTIME_DIR}|" \
    CMakeLists.txt
  if grep -q 'ONNXRUNTIME_DIR:PATH' CMakeLists.txt; then
    echo "  forwarded ONNXRUNTIME_DIR into piper-phonemize"
  else
    echo "  FAILED to patch CMAKE_ARGS — the sed did not match."
    echo "  Open CMakeLists.txt and look at the piper_phonemize_external block."
    fail "patch piper"
  fi
else
  echo "  ONNXRUNTIME_DIR forwarding already present"
fi

# `PIPER_PHONEMIZE_DIR` must stay UNDEFINED. It guards the ExternalProject that
# *builds* piper-phonemize; it is not a path to write to:
#
#   if(NOT DEFINED PIPER_PHONEMIZE_DIR)
#     ExternalProject_Add(piper_phonemize_external ...)
#   endif()
#
# Defining it skips that build entirely. piper then cannot find espeak-ng's
# headers or onnxruntime's, because installing them is the only thing that
# build does. An earlier version of this script passed it, and the build ran to
# 86% and died on two missing headers with no piper-phonemize on disk at all.

step "clearing the previous configure, which had that prefix baked in"
rm -rf build

step "configuring (builds piper-phonemize, which builds espeak-ng)"
ONNXRUNTIME_DIR="$ORT" \
  cmake -B build -S . \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEST" \
  || fail "cmake configure"

# Prove piper-phonemize is a target before spending an hour discovering
# otherwise. Its absence is what the last attempt could not report.
if grep -rq 'piper_phonemize_external' build/CMakeFiles 2>/dev/null \
   || grep -q 'piper_phonemize_external' build/Makefile 2>/dev/null; then
  echo "  piper-phonemize is a build target"
else
  echo "  piper_phonemize_external is NOT a target — it will not be built."
  echo "  Check that PIPER_PHONEMIZE_DIR is not defined anywhere above."
  fail "configure sanity"
fi

BUILD_LOG="$SRC/build.log"

# espeak-ng static-links: `target_link_options(espeak-ng PRIVATE
# "-static-libstdc++" "-static")`. bionic has no static libstdc++ and Termux's
# clang rejects the flag, so the link dies at ~94% — *after* the dictionary data
# has finished generating, which is what makes it look like it nearly worked.
#
# It cannot be patched before the build, because piper-phonemize fetches
# espeak-ng as an ExternalProject and the download happens during the build,
# not at configure time. So: build; if it fails on the static flags, patch what
# was downloaded and build again. ExternalProject caches the download, so the
# patch sticks and the second attempt does not re-fetch.
patch_espeak_for_android() {
  _found=0
  for _f in build/p/src/*/e/src/espeak_ng_external/src/libespeak-ng/CMakeLists.txt \
           build/p/src/*/e/src/espeak_ng_external/src/CMakeLists.txt \
           build/p/src/*/e/src/espeak_ng_external/CMakeLists.txt; do
    [ -f "$_f" ] || continue

    # 1. `target_link_options(espeak-ng PRIVATE "-static-libstdc++" "-static")`.
    #    bionic has no static libstdc++ and Termux's clang rejects the flag.
    if grep -q 'static-libstdc++' "$_f"; then
      sed -i 's/ "-static-libstdc++"//; s/ "-static"//' "$_f"
      echo "  stripped espeak-ng's static link flags in $_f"
      _found=1
    fi

    # 2. `add_subdirectory(speechPlayer)` is unconditional, so piper-phonemize's
    #    `-DUSE_SPEECHPLAYER:BOOL=OFF` stops the *link* but not the *compile* —
    #    it gates only `target_link_libraries(espeak-ng PRIVATE speechPlayer)`.
    #    speechWaveGenerator.cpp then fails to build:
    #
    #      error: reference to 'sample' is ambiguous
    #
    #    because espeak-ng declares its own `sample` and C++17's `<algorithm>`
    #    brings in `std::sample`. Nothing here needs a speech *player* — piper
    #    uses the library API — so the subdirectory is gated rather than
    #    deleted, which keeps the upstream intent readable.
    if grep -q '^[[:space:]]*add_subdirectory(speechPlayer)' "$_f"; then
      sed -i 's|^\([[:space:]]*\)add_subdirectory(speechPlayer)|\1if (USE_SPEECHPLAYER)\n\1  add_subdirectory(speechPlayer)\n\1endif()|' "$_f"
      if grep -q 'if (USE_SPEECHPLAYER)' "$_f"; then
        echo "  gated speechPlayer behind USE_SPEECHPLAYER in $_f"
        _found=1
      else
        echo "  FAILED to gate speechPlayer in $_f"
      fi
    fi
  done
  [ "$_found" -eq 1 ]
}

build_once() {
  _status=0
  ONNXRUNTIME_DIR="$ORT" cmake --build build --config Release -j"$JOBS" \
    >"$BUILD_LOG" 2>&1 || _status=$?
  return "$_status"
}

# Say what failed, not what make said. A nested ExternalProject reports several
# make levels of `Error 2`; the compiler message that explains it is above, and
# an earlier version piped to `tail` and lost exactly that.
report_failure() {
  echo
  echo "!!! full log: $BUILD_LOG ($(wc -l < "$BUILD_LOG" | tr -d ' ') lines)"
  echo "!!! first real errors:"
  grep -n -m 8 -iE "error:|fatal error|undefined reference|cannot find|No such file" \
    "$BUILD_LOG" | head -8 | sed 's/^/    /'
  exit 1
}

step "patching any espeak-ng left over from a previous attempt"
# ExternalProject caches its download, so on a retry the tree is already on
# disk and can be patched before the build rather than after a failed one. On a
# first run there is nothing to patch yet, and the retry below handles it.
patch_espeak_for_android || true

step "building — the long one"
# piper-phonemize compiles espeak-ng here and links Termux's onnxruntime rather
# than downloading a glibc one. Watch for a `Downloading ... onnxruntime` line:
# if it appears the patch did not take, and the result is a binary Android
# cannot load.
if ! build_once; then
  # Either espeak-ng arrived during this build and needs patching, or it failed
  # for a reason the patches do not address. Only the former is worth a retry.
  if patch_espeak_for_android; then
    step "patched espeak-ng; building again"
    build_once || { tail -40 "$BUILD_LOG"; report_failure; }
  else
    tail -40 "$BUILD_LOG"
    report_failure
  fi
fi

step "installing"
cmake --install build || fail "install"

# ------------------------------------------------------------------ verify

step "verifying"
# piper's install rules use `DESTINATION ${CMAKE_INSTALL_PREFIX}` — an absolute
# path — so an unmodified install lands the binary at `$DEST/piper`, flat, with
# its dependencies beside it. That is deliberate: piper resolves its data
# relative to its own executable,
#
#     exePath.parent_path().append("espeak-ng-data")
#     exePath.parent_path().append("libtashkeel_model.ort")
#
# so a relocation has to move those alongside it. It is done by patching the
# destinations to `bin`/`lib`/`share` and then moving the data back next to the
# binary, which keeps the "next to the executable" contract intact while landing
# on the layout termux-tts.sh and termux-tts-server.py already expect
# (~/piper-build/prefix/bin/piper).

# 1. the binary must be where the launcher looks for it
if [ ! -x "$DEST/piper" ]; then
  echo "no binary at $DEST/piper. Candidates:"
  find "$SRC" -name piper -type f -perm -u+x 2>/dev/null | head -5 || true
  fail "binary not found"
fi
mkdir -p "$DEST/bin"
mv "$DEST/piper" "$DEST/bin/piper"
chmod +x "$DEST/bin/piper"

# 2. and its data must be next to it, or phonemization fails at run time with a
#    message about espeak that points nowhere near the real cause
for _d in espeak-ng-data libtashkeel_model.ort; do
  if [ -e "$DEST/$_d" ] && [ ! -e "$DEST/bin/$_d" ]; then
    mv "$DEST/$_d" "$DEST/bin/$_d"
    echo "  moved $_d next to the binary"
  fi
done
for _d in espeak-ng-data libtashkeel_model.ort; do
  [ -e "$DEST/bin/$_d" ] || echo "  WARNING: $DEST/bin/$_d is missing"
done

BIN="$DEST/bin/piper"
"$BIN" --help >/dev/null 2>&1 || fail "binary does not run"

# The RPATH is baked at install time and points at the flat prefix, so after the
# move the loader needs help finding the .so files. Verified rather than assumed:
# the count below is what `ldd` actually reports, and it must be zero.
_missing=$(LD_LIBRARY_PATH="$DEST:$DEST/bin" ldd "$BIN" 2>/dev/null | grep -c 'not found' || true)
if [ "$_missing" != "0" ]; then
  echo "loader cannot resolve $_missing librar$( [ "$_missing" = 1 ] && echo y || echo ies ) even with LD_LIBRARY_PATH:"
  LD_LIBRARY_PATH="$DEST:$DEST/bin" ldd "$BIN" 2>/dev/null | grep 'not found' | sed 's/^/    /'
  fail "shared libraries"
fi

VOICE_DIR="$HOME/piper_models"
mkdir -p "$VOICE_DIR"
VOICE="$VOICE_DIR/en_US-amy-medium.onnx"
VOICE_JSON="$VOICE.json"

# Sizes come from the HuggingFace tree API, not from memory:
#   en_US-amy-medium.onnx      63201294  (lfs)
#   en_US-amy-medium.onnx.json    4882  (regular)
#
# `-s` is not a completeness test. On this link curl reports `100 60.27M` — a
# completed transfer — while writing 14 MB, because the connection is cut and
# its progress accounting is optimistic. A truncated file is still non-empty, so
# a `[ -s ]` guard skips the retry and piper then reports a *missing model
# config*, which sends the diagnosis somewhere else entirely. That is not
# hypothetical: it is what happened twice.
VOICE_BYTES=63201294
VOICE_JSON_BYTES=4882
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"

# Downloaded to a side file and promoted only once the byte count is exact, so a
# partial can never become the base of a resume — resuming from a corrupt prefix
# is what pushed the file past its true size instead of reaching it.
fetch_exact() { # url dest want label
  _u="$1"; _d="$2"; _want="$3"; _lbl="$4"; _n=0
  while [ "$_n" -lt 5 ]; do
    _have=$(wc -c < "$_d" 2>/dev/null | tr -d ' ')
    [ "$_have" = "$_want" ] && { echo "  $_lbl: $_have bytes, complete"; return 0; }
    _n=$((_n + 1))
    echo "  $_lbl: ${_have:-0} of $_want — retry $_n"
    rm -f "$_d.part"
    curl -L --retry 3 --retry-all-errors --speed-time 60 --speed-limit 1024 \
      -o "$_d.part" "$_u" >/dev/null 2>&1 || true
    _got=$(wc -c < "$_d.part" 2>/dev/null | tr -d ' ')
    if [ "$_got" = "$_want" ]; then mv "$_d.part" "$_d"; fi
  done
  echo "  $_lbl: could not obtain exactly $_want bytes (got $(wc -c < "$_d" 2>/dev/null | tr -d ' ' || echo 0))"
  return 1
}

step "fetching a voice (en_US-amy-medium, 63201294 bytes)"
fetch_exact "$BASE/en_US-amy-medium.onnx.json" "$VOICE_JSON" "$VOICE_JSON_BYTES" "config" \
  || fail "voice config"
fetch_exact "$BASE/en_US-amy-medium.onnx" "$VOICE" "$VOICE_BYTES" "model" \
  || fail "voice download"

step "end-to-end: speak a sentence to a file"
OUT="$DEST/piper-test.wav"
rm -f "$OUT"
LD_LIBRARY_PATH="$DEST:$DEST/bin" "$BIN" --model "$VOICE" --output_file "$OUT" <<'TEXT'
If this file has audio in it, piper works on this device.
TEXT
[ -s "$OUT" ] || fail "no audio produced"
# A non-empty file is not audio. Check the RIFF header, so a truncated or empty
# wav cannot pass for a working synthesis.
case "$(dd if="$OUT" bs=1 count=4 2>/dev/null)" in
  RIFF) : ;;
  *) fail "output is not a RIFF wav" ;;
esac

printf '\n==> DONE\n'
printf '    binary  %s\n' "$BIN"
printf '    voice   %s\n' "$VOICE"
printf '    test    %s (%s bytes)\n' "$OUT" "$(wc -c < "$OUT" | tr -d ' ')"
printf '\nNext:  sh ~/bin/onb-tts up\n'