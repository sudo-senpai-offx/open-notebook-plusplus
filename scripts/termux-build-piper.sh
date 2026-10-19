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
# directly. Termux packages onnxruntime 1.31.0, built for bionic, installing
# `lib/libonnxruntime.so` and the C API headers under `$PREFIX`. So
# `ONNXRUNTIME_DIR=$PREFIX` points a glibc download at a bionic build.
#
# Version skew — 1.14.1 against 1.31.0 — is the risk in this. piper-phonemize
# uses only the ONNX C API, which Microsoft has kept stable across those
# releases, so it should link. If it does not, the linker will name the missing
# symbol, and that symbol is the answer.
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
PREFIX="${PREFIX:-$HOME/piper-build/prefix}"
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
# actually link against rather than for the command being on PATH.
if [ ! -f "$PREFIX/lib/libonnxruntime.so" ] && [ ! -f /data/data/com.termux/files/usr/lib/libonnxruntime.so ]; then
  echo "onnxruntime not found."
  echo "  pkg install onnxruntime"
  echo "  (NOT python-onnxruntime — piper needs the C library and headers,"
  echo "   not the Python bindings)"
  fail "onnxruntime"
fi
ORT=$(pkg-config --variable=prefix onnxruntime 2>/dev/null || echo /data/data/com.termux/files/usr)
[ -f "$ORT/lib/libonnxruntime.so" ] || ORT="${PREFIX%%/piper-build*}/../.."
echo "cmake, git, pkg-config present"
echo "onnxruntime prefix: $ORT"
[ -f "$ORT/lib/libonnxruntime.so" ] || {
  echo "libonnxruntime.so still not found under $ORT"
  ls -la "$ORT/lib" 2>/dev/null | head -20 || true
  fail "onnxruntime library lookup"
}

mkdir -p "$PREFIX"

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
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
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
BIN="$PREFIX/bin/piper"
if [ ! -x "$BIN" ]; then
  echo "no binary at $BIN. Candidates:"
  find "$SRC" -name piper -type f -perm -u+x 2>/dev/null | head -5 || true
  fail "binary not found"
fi
"$BIN" --help >/dev/null 2>&1 || fail "binary does not run"

VOICE_DIR="$HOME/piper_models"
mkdir -p "$VOICE_DIR"
if [ ! -s "$VOICE_DIR/en_US-amy-medium.onnx" ]; then
  step "fetching a voice (en_US-amy-medium, ~63 MB)"
  BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"
  curl -L -C - --retry 5 --retry-all-errors -o "$VOICE_DIR/en_US-amy-medium.onnx" \
    "$BASE/en_US-amy-medium.onnx" || fail "voice download"
  curl -L -C - --retry 5 --retry-all-errors -o "$VOICE_DIR/en_US-amy-medium.onnx.json" \
    "$BASE/en_US-amy-medium.onnx.json" || fail "voice config"
fi

step "end-to-end: speak a sentence to a file"
"$BIN" --model "$VOICE_DIR/en_US-amy-medium.onnx" \
  --output_file "$PREFIX/piper-test.wav" <<'TEXT'
If this file has audio in it, piper works on this device.
TEXT
[ -s "$PREFIX/piper-test.wav" ] || fail "no audio produced"

printf '\n==> DONE\n'
printf '    binary  %s\n' "$BIN"
printf '    voice   %s\n' "$VOICE_DIR/en_US-amy-medium.onnx"
printf '    test    %s (%s bytes)\n' "$PREFIX/piper-test.wav" \
  "$(wc -c < "$PREFIX/piper-test.wav" | tr -d ' ')"
printf '\nNext:  sh ~/bin/onb-tts up\n'