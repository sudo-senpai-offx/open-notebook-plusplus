# Research T1 findings — no-AVX llama.cpp SSE2 sidecar

Parent ticket: `docs/7-DEVELOPMENT/wayfinder-student-os/tickets/T1-llamacpp-sse2-sidecar.md`.
Scope: Pentium D / Athlon 64 (SSE2/SSE3, 2 threads, 2 GB RAM), Qwen2.5-0.5B / SmolLM2 / TinyLlama quants, Docker sidecar.
Date: 2026-10-11. All claims cite primary sources; estimates are labelled as such.

## 1. Upstream fix: PR #12871 / issue #12866

- Issue: default builds executed AVX without a CPU check → `trap invalid opcode` / segfault in
  `libggml-cpu.so` / `libggml-base.so` on older x86 hardware.
  Source: https://github.com/ggml-org/llama.cpp/issues/12866
- Fix (merged 2025-04-21, `slaren`, approved by `ggerganov`): two commits —
  `ggml: add SSE 4.2 variant for CPUs without AVX` + `ggml: add x64 base ABI variant`.
  `Fixes #12866`.
  Source: https://github.com/ggml-org/llama.cpp/pull/12871
- Exact variant matrix added (`ggml/src/CMakeLists.txt`), i.e. the current build-variant names:
  `x64` (no extra flags), `sse42` (SSE42), `sandybridge` (SSE42 AVX),
  `haswell`, `skylakex`, `icelake`, `alderlake`, (+ `sapphirerapids` non-MSVC).
  Source: https://github.com/ggml-org/llama.cpp/pull/12871/files
- CMake mechanism (`ggml/src/ggml-cpu/CMakeLists.txt`): per-variant `ARCH_FLAGS`/`ARCH_DEFINITIONS`
  (`-msse4.2`/`GGML_SSE42`, `-mavx`, `-mavx2`, …); feature-detection objects built
  `-fno-lto` so LTO cannot inline newer ISA into the dispatch path.
  Source: https://raw.githubusercontent.com/ggml-org/llama.cpp/master/ggml/src/ggml-cpu/CMakeLists.txt
- Critical historical detail from the same diff: **before** this PR the non-MSVC fallback
  unconditionally appended `-msse4.2`; **after**, it is conditional on `GGML_SSE42`.
  Consequence: `GGML_NATIVE=OFF` alone was **not** sufficient pre-Apr-2025; on any checkout
  containing #12871 it yields an SSE2-compatible baseline when `GGML_SSE42=OFF`.
  Source: https://github.com/ggml-org/llama.cpp/pull/12871/files
- RamaLama precedent noted in the issue thread: RamaLama builds llama.cpp with `-DGGML_NATIVE=OFF`
  (collaborator `ericcurtin` comment on #12866).
  Source: https://github.com/ggml-org/llama.cpp/issues/12866

## 2. NeuralXP recipe (Pentium M floor, stricter than our target)

Repo: https://github.com/panmauk/NeuralXP (targets Dell Inspiron 6000, Pentium M, 2 GB RAM).

- Compiler flags (README "Why Pentium M / SSE2 only?"):
  `-march=pentium-m -mno-sse3`, plus `-DGGML_BMI2=OFF -DGGML_CPU_ALL_VARIANTS=OFF`,
  else "illegal instruction" on real hardware.
  Source: https://github.com/panmauk/NeuralXP
- Full CMake configure (`scripts/build-xp32.sh`) — exact flags:
  `-DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF
  -DGGML_SSE42=OFF -DGGML_SSSE3=OFF -DGGML_SSE3=OFF -DGGML_SSE2=ON -DBUILD_SHARED_LIBS=OFF`,
  `-DLLAMA_CURL=OFF -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_EXAMPLES=ON`,
  `-DCMAKE_BUILD_TYPE=Release`, MinGW Makefiles, XP toolchain file.
  Source: https://raw.githubusercontent.com/panmauk/NeuralXP/main/scripts/build-xp32.sh
- Toolchain (`cmake/xp32-toolchain.cmake`): `i686`, `_WIN32_WINNT=0x0501` / `WINVER=0x0501`,
  MinGW-w64 MSVCRT.
  Source: https://raw.githubusercontent.com/panmauk/NeuralXP/main/cmake/xp32-toolchain.cmake
- Model choice (`scripts/download-model.sh`): primary `Qwen2.5-0.5B-Instruct Q2_K`
  from `https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF`
  (`qwen2.5-0.5b-instruct-q2_k.gguf`); fallback SmolLM-135M `Q4_K_M` (~105 MB).
  Source: https://raw.githubusercontent.com/panmauk/NeuralXP/main/scripts/download-model.sh
- Achieved performance: **~30 s per short answer on Pentium M @ 1.6 GHz**, Windows XP SP2/SP3 32-bit.
  No tokens/sec number is published — any tok/s is derived (see §5).
  Source: https://github.com/panmauk/NeuralXP

## 3. Current portable-build + Docker procedure

- Portable CPU build: `cmake -B build` / `cmake --build build --config Release`;
  static with `-DBUILD_SHARED_LIBS=OFF`; curl off with `-DLLAMA_CURL=OFF`.
  Source: https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md
- `GGML_NATIVE=OFF` = do not pass `-march=native` (build covers generic/known GPUs/CPUs instead
  of the build host). Stated for CUDA non-native builds; same flag governs the CPU `-march=native` path.
  Source: https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md
- Official CPU Docker image (`.devops/cpu.Dockerfile`, raw):
  build stage `ubuntu:24.04` + `gcc-14 g++-14 build-essential git cmake libssl-dev`,
  configure `-DGGML_NATIVE=OFF -DLLAMA_BUILD_TESTS=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON`;
  runtime `ubuntu:24.04` + `libgomp1 curl ffmpeg`; `server` target ships only `llama` + `llama-server`
  with `HEALTHCHECK curl -f http://localhost:8080/health`.
  Source: https://raw.githubusercontent.com/ggml-org/llama.cpp/master/.devops/cpu.Dockerfile
- Recommended sidecar adaptation: multi-stage `debian:bookworm-slim` (or `ubuntu:22.04`) runtime
  with `libgomp1` + `curl` only; build stage needs only `build-essential git cmake`.
  Two build options: (a) static floor build per NeuralXP flags (max compat, no runtime dispatch);
  (b) `-DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON` (one image, runtime picks `x64`/`sse42`/AVX…
  per host — includes the #12871 no-AVX variants).
- Binary size: **no primary source publishes one**. Upstream Dockerfiles copy `build/bin/*`
  without sizes; the Alpine third-party example strips `llama-server` to save space
  (pattern only, not upstream). Record actual `ls -la` + `du` of `llama-server`/`llama-cli`
  at build time as a T1 follow-up; rough expectation for a static stripped server binary is
  low tens of MB (ESTIMATE — measure, do not quote).
  Sources: https://raw.githubusercontent.com/ggml-org/llama.cpp/master/.devops/cpu.Dockerfile ;
  stripped-binary pattern via websearch `SamuelTallet/alpine-llama-cpp-server Dockerfile`.

## 4. RAM math per model (2 GB budget)

File sizes (primary: HF repo file listings):

| Model / quant | File | HF "Max RAM" note |
|---|---|---|
| Qwen2.5-0.5B Q2_K | **415 MB** | n/a (repo lists per-quant sizes) — https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF |
| Qwen2.5-0.5B Q4_K_M | **491 MB** | same source |
| SmolLM2-360M Q4_K_M | **271 MB**; Q2_K **219 MB** | https://huggingface.co/QuantFactory/SmolLM2-360M-Instruct-GGUF |
| SmolLM2-360M Q8_0 | **386 MB** | https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF |
| TinyLlama-1.1B Q2_K | **0.48 GB** → Max RAM **2.98 GB** | https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF |
| TinyLlama-1.1B Q4_K_M | **0.67 GB** → Max RAM **3.17 GB** | same source |

- KV-cache formula ( legitimates the "file × 1.5–3" heuristic): per token
  ≈ 2·layers·n_kv_heads·head_dim·2 B (fp16). Qwen2.5-0.5B arch (24 layers, GQA 14Q/2KV, hidden 896
  → head_dim 64): ≈ **12 KB/token** → ctx 2048 ≈ 25 MB, ctx 512 ≈ 6 MB.
  Arch figures source: https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF
  (24 layers, 14Q/2KV heads; full ctx 32768 — we run a fraction of it).
- Total RSS ≈ mmap'd weights + KV(ctx) + compute buffer (~100–300 MB) + server overhead.
  TheBloke "Max RAM" column assumes large default ctx, so TinyLlama Q2_K's 2.98 GB
  already exceeds the 2 GB box at defaults.
- Verdict for 2 GB: **Qwen2.5-0.5B Q2_K @ ctx ≤ 1024 fits** (~415 + ~12 + ~200–300 MB ≈ 0.7 GB);
  **SmolLM2-360M Q4_K_M fits comfortably** (~271 MB + small KV); **TinyLlama-1.1B does NOT fit
  at default ctx** — only viable with tiny ctx (≤ 512) + `--mlock` off + swap, i.e. deprioritise it.
  Keep server ctx small (`-c 512/1024`), `--n-gpu-layers 0`, single sequence.
  Example invocation pattern (`-c 2048` default in examples; reduce for this box):
  https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF

## 5. Speed expectation (2 threads, no AVX)

- Only primary data point: NeuralXP ~30 s/short answer on 1.6 GHz Pentium M.
  Source: https://github.com/panmauk/NeuralXP
- Derived ESTIMATE (not a benchmark): a "short answer" ≈ 60–150 tokens → **≈ 2–5 tok/s**
  single-thread Pentium M class; 2 threads on Pentium D/Athlon 64 ≈ same order, **plan for 2–6 tok/s**,
  prompt processing slower on long prompts. No primary tok/s exists for this exact CPU —
  first build must record `llama-bench`/`llama-server` timings.
- Keep prompts short and `n_predict` capped; this is a fallback path, not the default.

## 6. Compose wiring sketch (opt-in, env-gated; host Ollama stays default)

Grounded in current repo files:
`docker-compose.yml` in `/home/dev-sudo-senpai/open-notebook/docker-compose.yml`
(whisper/piper already use `profiles: ["media"]`);
routing hook `open_notebook/ai/provision.py` → `FORCE_MINI` in `open_notebook/utils/model_router.py`;
constraint `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` already in compose.

```yaml
# docker-compose.sse2.yml (new file, never in default up path)
services:
  llamacpp-sse2:
    build:
      context: ./sidecars/llamacpp-sse2
      dockerfile: Dockerfile.sse2
    image: llamacpp-sse2:local
    profiles: ["sse2"]              # opt-in: docker compose --profile sse2 up
    ports: ["127.0.0.1:8099:8080"]
    environment:
      - LLAMA_ARG_MODEL=/models/qwen2.5-0.5b-instruct-q2_k.gguf
      - LLAMA_ARG_CTX_SIZE=1024
      - LLAMA_ARG_N_THREADS=2
      - LLAMA_ARG_N_GPU_LAYERS=0
    volumes:
      - ./sidecars/llamacpp-sse2/models:/models:ro
    restart: "no"                   # never competes with worker heartbeats
```

```python
# open_notebook/ai/provision.py — behind existing FORCE_MINI only
ALLOW_SSE2 = os.getenv("ALLOW_LLAMACPP_SSE2", "false").lower() in ("1", "true")
if ALLOW_SSE2 and FORCE_MINI and os.getenv("LLAMACPP_SSE2_URL"):
    # route mini chat/transform calls to LLAMACPP_SSE2_URL (OpenAI-compatible /v1),
    # else fall through to host Ollama (OLLAMA_API_BASE) unchanged.
```

- `OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` untouched; sidecar is HTTP-called, never in-process torch.
- Health: reuse upstream `curl -f http://localhost:8080/health` pattern.

## 7. Contradictions / corrections to the starting assumptions

1. **`GGML_NATIVE=OFF` alone is version-dependent.** Pre-#12871 it still forced `-msse4.2`
   (see PR diff); only post-#12871 checkouts get a true SSE2 baseline. Pin or verify the
   llama.cpp revision ≥ 2025-04-21. Source: https://github.com/ggml-org/llama.cpp/pull/12871/files
2. **NeuralXP README vs its own script disagree.** README says `-march=pentium-m -mno-sse3`;
   `build-xp32.sh` instead disables whole feature list (SSE3/SSSE3/SSE42/FMA/F16C/AVX/AVX2, SSE2 ON)
   and the toolchain targets generic `i686`. For our 64-bit Docker sidecar, use
   `-march=x86-64 -mno-sse3`-style baseline (Pentium D lacks SSE4; Athlon 64 lacks SSE3 on early steppings),
   not `-march=pentium-m` (32-bit). Sources: https://github.com/panmauk/NeuralXP ;
   https://raw.githubusercontent.com/panmauk/NeuralXP/main/scripts/build-xp32.sh
3. **GCC version conflict in NeuralXP:** README prescribes GCC 10.5.0/MSVCRT ("sweet spot",
   GCC 15+ uses Vista+ APIs); the script hardcodes a winlibs GCC 15.2.0 path. Linux Docker
   avoids this entirely (use distro gcc-12/13/14 as upstream does).
   Sources: https://github.com/panmauk/NeuralXP ;
   https://raw.githubusercontent.com/ggml-org/llama.cpp/master/.devops/cpu.Dockerfile
4. **Qwen Q2_K size:** NeuralXP script comment says "~300 MB" but HF lists **415 MB** and its own
   README says "the model alone is 415 MB". Budget 415 MB, not 300 MB.
   Sources: https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF ;
   https://raw.githubusercontent.com/panmauk/NeuralXP/main/scripts/download-model.sh
5. **TinyLlama-1.1B is effectively out** at 2 GB under default ctx (0.48 GB file → 2.98 GB max RAM
   per TheBloke table). Prefer SmolLM2-360M as the smallest fallback, Qwen2.5-0.5B-Q2_K as primary.
   Source: https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF
6. **`GGML_CPU_ALL_VARIANTS=OFF` (NeuralXP) vs `ON` (upstream Docker):** OFF gives the smallest,
   guaranteed-old-CPU binary; ON (with #12871) gives one portable image with runtime dispatch
   down to the `x64` baseline. Recommend ON for the Docker sidecar unless the binary must also
   run on Pentium M (no SSE3) — then use the NeuralXP-style full OFF floor.
   Sources: https://raw.githubusercontent.com/panmauk/NeuralXP/main/scripts/build-xp32.sh ;
   https://raw.githubusercontent.com/ggml-org/llama.cpp/master/.devops/cpu.Dockerfile
7. **No primary tok/s or binary-size figures exist** for this exact target; §3/§5 mark them
   ESTIMATE/TODO. Do not quote them as facts in T3–T6.
