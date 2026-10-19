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
# piper's CMakeLists adds `-static-libgcc -static-libstdc++` whenever the target
# is neither MSVC nor Apple — Android is neither, so Android gets a glibc-only
# flag set. bionic has no static libstdc++ and the link fails naming a file
# nobody has heard of rather than the CMakeLists line that caused it.
if grep -q 'static-libstdc++' CMakeLists.txt; then
  sed -i 's/ -static-libgcc -static-libstdc++//' CMakeLists.txt
  echo "removed -static-libgcc -static-libstdc++"
else
  echo "no static flags found — upstream may have changed this; re-read the CMakeLists"
fi

step "configuring (this fetches piper-phonemize, which builds espeak-ng)"
# ONNXRUNTIME_DIR goes in the *environment*, not on the command line. piper
# builds piper-phonemize with ExternalProject_Add and passes only
# CMAKE_INSTALL_PREFIX through, so a -D here would not reach the nested build.
# The environment does propagate, which is the only way to reach it.
ONNXRUNTIME_DIR="$ORT" \
  cmake -B build -S . \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DPIPER_PHONEMIZE_DIR="$SRC/phonemize" \
  || fail "cmake configure"

step "building — the long one"
# piper-phonemize compiles espeak-ng here. onnxruntime is NOT rebuilt: the
# ONNXRUNTIME_DIR above skipped its download, and it links Termux's.
ONNXRUNTIME_DIR="$ORT" cmake --build build --config Release -j"$JOBS" 2>&1 | tail -150 \
  || fail "build"

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