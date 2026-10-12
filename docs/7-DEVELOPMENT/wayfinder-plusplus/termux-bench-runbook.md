# SD680 Termux bench runbook (N1 follow-up — needs hands on-device)

Goal: honest tok/s + RSS for the floor ladder on your Snapdragon 680
(8GB LPDDR4X), then scale judgment toward the SD439 floor. Companion-server
stays the product; this measures the all-on-device experiment (vehicle 2).

## 0. On the phone (Termux from F-Droid, NOT Play Store)

```sh
pkg update && pkg install -y git cmake clang python
git clone --depth 1 --branch v0.6.0 https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
```

## 1. Portable ARM build (no AVX concepts exist here — plain NEON baseline)

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
  -DGGML_BACKEND_DL=OFF -DLLAMA_CURL=OFF \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF
cmake --build build --config Release -j$(nproc) --target llama-bench
```

## 2. Get one model on-device (resumable, verify size after)

```sh
mkdir -p ~/gguf && cd ~/gguf
# Qwen2.5-0.5B Q2_K (~415MB). Prefer Wi-Fi; expect slow eMMC writes.
curl -SL -C - --retry 5 --retry-all-errors \
  -o qwen2.5-0.5b-instruct-q2_k.gguf \
  https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q2_k.gguf
```

## 3. Bench (match the x86 matrix: 2 threads first, then all-core)

```sh
./build/bin/llama-bench -m ~/gguf/qwen2.5-0.5b-instruct-q2_k.gguf -t 2 -p 64 -n 16
./build/bin/llama-bench -m ~/gguf/qwen2.5-0.5b-instruct-q2_k.gguf -t 8 -p 512 -n 128
```

## 4. Report back (paste into chat)

- The pp/tg lines for both runs, `free -m` output, and device temp feel
  (warm/hot/throttled?) plus whether the phone stayed responsive.
- RSS: run `ps -o rss= -C llama-bench` mid-run if possible.

## 5. What it decides

- SD680 ≥ ~5 tok/s @8t → Termux experiment proceeds to FastAPI+SurrealDB
  proot step; SD439 floor judgment scales down from here (A53 ≈ 1/3 IPC).
- Below that → vehicle 2 stays lab-only; companion-server+PWA remains the
  ARM story and no further on-device work is scheduled.
