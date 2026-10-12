# CPU Inference Research — low-end targets (2-thread SSE2 x86 / Snapdragon 439 ARM)

Date: 2026-10-12. Scope: read-only research for open-notebook low-end tier
(2 threads, 2 GB RAM, SSE2/SSE3 x86 first, Snapdragon 439 ARM second;
current stack Qwen2.5-0.5B Q2_K + SmolLM2 fallback via llama.cpp, host Ollama for the rest).
All claims cite primary sources in §6.

## 1. Verdict summary

| Item | Verdict | Pin / action |
|---|---|---|
| IQ4_XS / IQ3_M / IQ2_M (mainline i-quants) for 0.5–0.6B models | **ADOPT** (bench vs Q2_K/Q4_K_M) | Quantize with llama.cpp ≥ b9885 (2026-07-06) + imatrix; pre-quantize externally, import to Ollama via `FROM` |
| IQ_K family (IQ2_K, IQ3_K, IQ4_K, IQ5_K, IQ2_KL, _R4/_KS) | **FLAG-GATE** (needs ik_llama.cpp fork) | ik_llama.cpp main (no version tags; last upstream sync claimed Aug 2024, but feature PRs continue into 2026 — pin by commit hash, verify Qwen3/SmolLM3 arch support before use) |
| IQ1_S / IQ1_M / Q1_0 / ternary 1-bit on sub-1B | **SKIP** | Quality collapse at sub-1B (see §2); Q1_0 CPU support exists upstream (#21273, Apr 2026) but not useful here |
| BitNet.cpp + BitNet-b1.58-2B-4T as default low-end runtime | **SKIP** | Keep一体 llama.cpp/Ollama path; BitNet needs clang≥18, separate runtime, no Ollama interop |
| BitNet.cpp as x86 experiment (I2_S 2B-4T) | **FLAG-GATE** | microsoft/BitNet main + `setup_env.py -q i2_s`; verify SSE2-only (non-AVX2) path first — official x86 claims assume AVX2 |
| Qwen3-0.6B-GGUF as Qwen2.5-0.5B successor | **ADOPT** (A/B bench required) | Qwen/Qwen3-0.6B-GGUF (official Q8_0) or bartowski/TensorBlock/NANI quant ladders; default candidates Q4_K_M (0.48 GB) and IQ4_XS (0.44–0.45 GB) |
| Qwen3.5-0.8B (qwen35 arch, hybrid DeltaNet) | **FLAG-GATE** | Requires llama.cpp with MTP merge (≥ 2026-05-16) + qwen35 arch; Ollama `qwen3.5:0.8B` only after host bump |
| SmolLM3-3B-GGUF for the 2 GB tier | **SKIP** | Q4_K_M alone is 1.92 GB — blows budget with KV cache + OS; server-side-only option |
| SmolLM2 135M/360M fallback | **ADOPT** (keep) | Ollama `smollm2:135m/:360m`, QuantFactory GGUF for direct llama.cpp |
| llama.cpp bump to ≥ b9885 + native SSE2-fallback build | **ADOPT** | Pin ≥ b9885 (2026-07-06); prebuilt `linux-x64` for safety, `-DGGML_NATIVE=ON` bench on the actual 2-thread box |

## 2. ik_llama.cpp IQ formats vs Q2_K/Q3_K on sub-1B models

### 2.1 What beats what per byte (mainline-available quants)

Best perplexity-per-byte table on Mistral-7B (Artefact2 gist, imatrix, wiki.test) —
ordering is by bits/weight, lower KL-divergence is better:

- IQ2_M: 2.76 bpw, median KLD 0.0702 — beats Q2_K_S (2.79 bpw, 0.0829) at smaller size.
- Q2_K: 3.00 bpw, 0.0588 — slightly better median than IQ2_M but costs ~0.24 bpw more.
- IQ3_S: 3.52 bpw, 0.0205 — clearly beats Q3_K_S (3.50 bpw, 0.0304) at equal size.
- IQ3_M: 3.63 bpw, 0.0186 vs Q3_K_M 3.89 bpw, 0.0171 — ~equal quality at ~0.26 bpw saving.
- IQ4_XS: 4.32 bpw, 0.0088 ≈ Q4_K_S (4.57 bpw, 0.0083) — same quality class, ~0.25 bpw smaller.
- IQ4_NL: 4.56 bpw, 0.0085 ≈ Q4_K_S at equal size.

So for the sizes that matter on a 2 GB box: **IQ3_S > Q3_K_S,
IQ3_M ≈ Q3_K_M (smaller), IQ4_XS ≈ Q4_K_S (smaller), IQ2_M > Q2_K_S**.
Q4_K_M (4.83 bpw, 0.0075) remains the quality default if it fits — and at
0.5–0.6B it does (~0.40–0.48 GB).

### 2.2 Why IQ_K (ik-exclusive) matters and why it is gated

ik_llama.cpp Discussion #8 shows the mechanism and the quality deltas:

- Sub-4-bit i-quants use codebooks (large LUT lookups) → big CPU penalty.
  IQ_K quants replace the codebook with a 2×N small non-linear LUT
  (4/8/16/32 INT8 entries) that fits in 1–2 SIMD registers
  (`_mm256_shuffle_epi8` on AVX2, `vqtbl1q_s8` on NEON) → near-K-quant CPU speed.
- Quality: IQ4_K quantization error ~2.7× lower than Q4_0 and ~40% lower than
  Q4_K_S; IQ5_K ~2.1× lower than Q5_0, ~40% lower than Q5_K_S (LLaMA-3.1-70B PPL).
- Speed (Ryzen AVX2, LLaMA-3.1-8B): IQ3_K prompt-processing 6.45× faster than
  upstream IQ3_S (180.8 vs 28.0 t/s), token-gen 2.37× (16.1 vs 6.8 t/s).
  NEON gains are smaller (1.1–1.8×).
- IQ2_KL (PR #602, 2.6875 bpw, blocks-of-32 for CUDA PP) "totally obliterates"
  same-size Q2_K (2.625 bpw) on quality at equal CPU speed.

Gate reasons: **IQ2_K / IQ3_K / IQ4_K / IQ5_K / IQ6_K / IQ2_KL / _KS / _R4 are
ik_llama.cpp-exclusive** — upstream `tools/quantize/quantize.cpp`
(QUANT_OPTIONS) lists only IQ2_XXS/XS/S/M, IQ1_S/M, TQ1_0/TQ2_0, IQ3_XXS/S/M/XS,
IQ4_NL/XS (plus Q1_0/Q2_0). Ollama tracks upstream, so IQ_K files cannot be
served through the "host Ollama" path. Adopting IQ_K means vendoring the
ik_llama.cpp fork (whose README claims last upstream sync Aug 2024 yet lists
feature PRs into 2026 — pin by commit, verify Qwen3/SmolLM3/qwen35 arch
coverage, and confirm the SSE2-only fallback path, since the IQ_K SIMD fast
path is written for AVX2/NEON).

### 2.3 Floor: below ~3 bpw is not useful on modern small models

LLaMA-3.2-3B evaluation (ik Discussion #63): quantization error explodes below
3 bpw; at 3.68 bpw IQ3_K already matches Q4_0's error. For 0.5–0.6B models with
less redundancy than 70B, treat **IQ2_M (~2.7 bpw) as the usability floor**,
IQ2_XS/XXS and IQ1_* as experimental only. This matches community quant notes
(NANI-Nithin Qwen3-0.6B card: "tight on memory: IQ4_XS, then IQ3_M, then
IQ2_M"; "IQ2_XS and below: experimental").

### 2.4 RAM / speed expectations (0.5–0.6B, 2 threads, SSE2-only)

- File sizes (imatrix, same BF16 source): Qwen2.5-0.5B Q2_K 339 MB / IQ2_M
  329 MB (bartowski b3772); Qwen3-0.6B Q2_K 0.347 GB, Q4_K_M 0.484 GB,
  IQ4_XS ~0.44–0.45 GB, IQ3_M ~0.38–0.40 GB, IQ2_M ~0.31–0.33 GB
  (TensorBlock / bartowski / NANI-Nithin cards).
- All fit in 2 GB with KV cache to spare: Qwen3-0.6B is 28 layers, GQA 16Q/8KV —
  a 512–2048-token Q8 KV cache is tens of MB.
- Speed: no published 2-thread SSE2-only numbers exist for these exact quants.
  Anchors: (a) Jan-2025 ik wiki — ik is 2.9–3.35× (geomean) faster than upstream
  on prompt processing, so upstream-only benches understate IQ_K; (b) a
  Core 2 Duo E8400 (2C, SSE-era) runs a 4B model at ~2 t/s, implying a 0.5B
  model at roughly an order of magnitude faster (~15–40 t/s PP, ~10–25 t/s TG
  estimated). **Must bench on the actual box**: `llama-bench -t 2` for
  Q2_K vs IQ2_M vs IQ3_M vs IQ4_XS, threads 1–2, before changing the default.
- SSE2 caveat: the IQ_K shuffle fast path needs AVX2 (`_mm256_shuffle_epi8`);
  on SSE2-only the fallback is scalar/slower, which shrinks IQ_K's speed edge
  and favors K-quants on this specific x86 target. I-quants (codebook LUTs)
  are the slowest on weak CPUs — prefer IQ4_XS/IQ3_M over IQ2_XS/XXS here.

## 3. BitNet.cpp — current status and ARM story

### 3.1 Status (as of mid-2026)

- Official 1-bit model BitNet-b1.58-2B-4T (2.4B params, 4T tokens) released
  2025-04-14; official GPU kernel 2025-05-20; CPU inference optimization
  (parallel kernels, configurable tiling, embedding quantization, +1.15–2.1×
  over the original) 2026-01-15; 1-bit embedding models (0.6B/270M, x86 I2_S)
  and VibeASR.cpp (AVX2 + NEON, RTF<1 on few threads) July 2026.
- Claimed speedups vs full precision: 2.37–6.17× on x86, 1.37–5.07× on ARM;
  energy −71.9–82.2% x86, −55.4–70.0% ARM (README + tech report arXiv:2410.16144).
- Smallest *generative* options: community bitnet_b1_58-large 0.7B and
  bitnet_b1_58-3B (research-era, ~100B tokens), Falcon 1B/3B 1.58-bit family,
  Llama3-8B-1.58-100B-tokens — plus the official 2.4B. No 0.5B-class official
  instruct model exists. (Embedding 270M/0.6B are not chat models.)

### 3.2 Why SKIP-by-default / FLAG-GATE as experiment

1. Separate runtime, separate format: custom ggml fork under `3rdparty/`,
   `setup_env.py -md <dir> -q i2_s` conversion to `ggml-model-i2_s.gguf`,
   `run_inference.py`. Not loadable in Ollama, not interchangeable with GGUF
   K/IQ quants. Doubles the inference backend surface for the low-end tier.
2. Toolchain: requires python≥3.10, cmake≥3.22, **clang≥18** — heavier than the
   llama.cpp gcc/MSVC build, painful on a 2-thread box.
3. x86 baseline mismatch: all official x86 claims and VibeASR target **AVX2**;
   an SSE2/SSE3-only CPU has no verified fast path — expect fallback or failure
   until proven otherwise on the box.
4. ARM story is NEON-with-caveats: support matrix shows I2_S ✅ on ARM for the
   2B-4T model (TL1 is the ARM kernel column), and embeddings are **x86-only
   (ARM ❌)**. But ARM backend fragility is documented — issue #556
   (Windows-on-ARM Snapdragon X) reports regressions producing "token salad",
   fixed only by pinning to commit `404980e` plus manual `chrono` includes and
   hand-set i8mm/dotprod flags. Snapdragon 439 is 8× Cortex-A53 (ARMv8-A,
   plain NEON, **no dotprod/i8mm/SVE**) — the lowest-performance NEON target,
   in-order cores, so realized speedups will sit far below the advertised
   ranges (measured on Apple M2 / modern cores). It should *run* via the
   generic NEON path, but budget for "slow but correct", not the 5× headline.
5. Fit note: 2.4B×2 bit ≈ 0.6 GB weights — it *fits* 2 GB. The blocker is not
   size but toolchain + quality evidence (no published perplexity-vs-Qwen-0.6B
   comparison on the tasks open-notebook cares about) + dual-backend cost.

Experiment gate (if pursued): pin microsoft/BitNet by commit, build with
clang≥18 on x86-64, convert BitNet-b1.58-2B-4T with `-q i2_s`, bench
`e2e_benchmark.py -p 256 -n 128 -t 2` against Qwen3-0.6B IQ4_XS at `-t 2`,
then repeat on the Snapdragon 439 board before any product decision.

## 4. Small dense releases with GGUF availability

### 4.1 Qwen3-0.6B — ADOPT as upgrade candidate (A/B bench first)

- Release 2025-04-29, Apache-2.0. 0.6B total / 0.44B non-embedding, 28 layers,
  GQA 16Q/8KV, 32K context, thinking/non-thinking in one model (`/think`,
  `/no_think`), 100+ languages.
- Official GGUF: Qwen/Qwen3-0.6B-GGUF (Q8_0, 639 MB); `ollama run
  hf.co/Qwen/Qwen3-0.6B-GGUF:Q8_0`. Full quant ladders from bartowski
  (incl. IQ4_XS/NL, IQ3_M/XS/XXS, IQ2_M, Q2_K_L, Q4_K_L…), TensorBlock
  (Q2_K 0.347 GB … Q8_0 0.805 GB), NANI-Nithin (30 files, BF16→imatrix,
  wikitext-2 calibration).
- Sampling note (official card): thinking mode temp 0.6 / top-p 0.95 / top-k 20
  / presence-penalty 1.5; **presence-penalty 1.5 specifically recommended for
  quantized models** to suppress repetition. Non-thinking: temp 0.7 / top-p 0.8.
- Cost vs Qwen2.5-0.5B: +20% params (~0.48 GB Q4_K_M vs ~0.40 GB), same 2 GB
  envelope. Gains: reasoning, agent/tool-call, multilingual. Risks: GQA-8 KV
  and 28 layers slightly raise per-token matmul vs 0.5B; thinking trace costs
  tokens — default `/no_think` + short `n_predict` on low-end.
- Candidates to bench: Q4_K_M (usual default), IQ4_XS (smaller, ≈Q4_K_S
  quality), Q3_K_M/IQ3_M (tight-RAM fallback). Keep Qwen2.5-0.5B Q2_K until the
  bench wins.

### 4.2 Qwen3.5-0.8B — FLAG-GATE (too new for the frozen low-end)

- Release 2026-02-24, Apache-2.0. 0.8B (+150M aux/vision), hybrid
  Gated-DeltaNet + Gated Attention (6×(3×DeltaNet→FFN→1×Attention→FFN)), 262K
  native context, multimodal, new `qwen35` arch.
- GGUF exists (unsloth MTP-GGUF, Rk1516 Q5_K_M, YoozLabs QAT Q4_0) and Ollama
  ships `qwen3.5:0.8b` (1.0 GB, 256K ctx). But: llama.cpp support required the
  MTP speculative-decoding merge (2026-05-16; `--spec-type` renamed
  `mtp`→`draft-mtp` 2026-05-13), and YoozLabs validates their Q4_0 build only
  to 8K context (regresses past that). DeltaNet kernels on SSE2-only 2-thread
  are unproven. Gate on: host llama.cpp/Ollama ≥ May-2026 code, then re-bench
  RAM (1.0 GB file + larger KV from 24 hybrid layers) against the 2 GB budget.

### 4.3 SmolLM3 — SKIP for 2 GB (server-side only)

- SmolLM3-3B (2025-07-08, Apache-2.0): 3B-only family member, 11.2T tokens,
  GQA + NoPE (NoRoPE), 64K→128K YaRN, 6 languages, hybrid reasoning. Official
  GGUF ggml-org/SmolLM3-3B-GGUF: Q4_K_M **1.92 GB**, Q8_0 3.28 GB, F16 6.16 GB.
  1.92 GB weights + KV + runtime > 2 GB — disqualified for the low-end tier by
  arithmetic. No sub-1B SmolLM3 exists (SmolLM2 135M/360M/1.7B remain the small
  options; Ollama `smollm2`, QuantFactory GGUF). ik_llama.cpp added SmolLM3
  support (PR #934), upstream needs `transformers≥4.53`-era code — fine for
  the *host* tier, not the 2 GB tier.

## 5. llama.cpp CPU-backend work 2025–2026 relevant to 2-thread SSE2-only

Nothing in 2025–2026 specifically targets 2-thread SSE2-only, but several
upstream changes are free gains on a version bump (pin ≥ b9885, 2026-07-06):

- **Q1_0 1-bit CPU support** (#21273, Apr 2026) + x86/generic `q1_0` dot
  optimization (#21636, Apr 2026); Metal/CUDA follow-up (`59f2b84`).
  Relevance: none for quality (see §2.3 floor) — listed so nobody re-tests it.
- **RMS_NORM+MUL fusion on CPU** (#22423, May 2026) + 64 B-aligned tile
  buffers (#21058): small per-token saving, applies to every decode step.
- **Flash-Attention CPU**: GEMM microkernel (#19422, Feb 2026), split-across-KV
  for faster token-gen (#19209, Feb 2026). Helps GQA decode (Qwen3-0.6B's
  16Q/8KV); on 2 threads the gain is modest but non-zero.
- **Tiled matmul** (b9885 note: ~2× PP speed for FP32/Q4_0/Q8_0): prompt-side
  only; token-gen (the 2-thread bottleneck) unaffected. Still take it.
- **Parallel quant-LUT init** (#23595, May 2026): faster startup, no steady-state
  effect. KleidiAI dynamic chunk scheduling (#23819, June 2026): targets
  hybrid big.LITTLE — Snapdragon 439 is homogeneous 8×A53, N/A.
- **Sampling**: ik_llama.cpp PR #1187 ("much faster RNG sampling") is the only
  called-out sampling win; upstream sampling cost is small relative to matmul
  at 0.5B, but it rides along with any bump. No cache-layout or threadpool
  redesign targeting 2-thread SSE2 was found in the surveyed releases/issues
  (searched: sampling speedup, cache layout, threadpool, SSE2 baseline).
- **SSE2 baseline reality**: x86-64 guarantees SSE2 (OSDev/StackOverflow), and
  llama.cpp advertises AVX/AVX2/AVX512/AMX x86 paths — SSE2-only gets the
  generic fallback. Practical consequences: (a) use the prebuilt `linux-x64`
  binary for correctness, (b) bench a `-DGGML_NATIVE=ON` build on the actual
  box for speed, (c) prefer K-quants over codebook i-quants on this target,
  (d) never enable OpenBLAS for this shape (known "more threads = slower"
  issue #5534; CPU backend beats BLAS on small models).
- **Ollama interop**: current Ollama imports any pre-quantized GGUF via
  Modelfile `FROM` (community IQ2_M/IQ3_M/IQ4_XS models on the registry prove
  runtime support), but `ollama create -q` only offers Q4_0/Q4_1/Q5_0/Q5_1/Q8_0
  + Q3_K_*/Q4_K_*/Q5_K_*/Q6_K — **i-quants must be quantized with llama.cpp
  first, then imported**. Keep the vendored `llama-quantize` + imatrix step.

## 6. Sources (primary, per claim)

- ik_llama.cpp repo / IQ_K design + perf tables: https://github.com/ikawrakow/ik_llama.cpp
  (README: fork with SOTA quants; Discussion #8: IQ_K codebook/LUT design, PPL
  deltas, AVX2/NEON tables; PR #602: IQ2_KL 2.6875 bpw; PR #461: CUDA R4;
  PR #1187: RNG sampling; PR #934 SmolLM3, #355 Qwen3, #337 BitNet model support)
- ik_llama.cpp Jan-2025 prompt-processing comparison (2.9–3.35× geomean):
  https://github.com/ikawrakow/ik_llama.cpp/wiki/Jan-2025:-prompt-processing-performance-comparison
- ik_llama.cpp LLaMA-3.2 quantization evaluation (<3 bpw not useful):
  https://github.com/ikawrakow/ik_llama.cpp/discussions/63
- KLD-vs-bpw table (Mistral-7B, imatrix): https://gist.github.com/Artefact2/b5f810600771265fc1e39442288e8ec9
- Upstream quant list (mainline has no IQ_K):
  https://github.com/ggml-org/llama.cpp/blob/master/tools/quantize/quantize.cpp
- Upstream CPU PRs: Q1_0 #21273 + #21636 (Apr 2026), FA #19422/#19209 (Feb 2026),
  RMSNORM+MUL #22423 + tile-align #21058 (Apr–May 2026), LUT-init #23595,
  KleidiAI #23819, tiled-matmul b9885 (2026-07-06): https://github.com/ggml-org/llama.cpp/releases
  (b9885/b8685/b10181 notes via newreleases.io mirrors)
- OpenBLAS more-threads-slower #5534: https://github.com/ggml-org/llama.cpp/issues/5534
- 2-core Core 2 Duo E8400 CPU-only guide (~2 t/s at 4B):
  https://gist.github.com/alexlib/d66403d73a29840d3d3766888724192e
- SSE2 x86-64 baseline: https://osdev.wiki/wiki/SSE ,
  https://stackoverflow.com/tags/sse2/info
- llama.cpp x86 ISA support (AVX/AVX2/AVX512/AMX) + Qwen3 needs b5092:
  https://qwen15.readthedocs.io/en/latest/run_locally/llama.cpp.html ,
  https://raw.githubusercontent.com/ggml-org/llama.cpp/HEAD/README.md
- BitNet.cpp README (models, I2_S/TL1 matrix, speedups, energy, clang≥18,
  setup_env/run_inference/e2e_benchmark): https://github.com/microsoft/BitNet
- BitNet tech reports: https://arxiv.org/abs/2410.16144 (1-bit inference),
  https://arxiv.org/abs/2402.17764 (b1.58), https://arxiv.org/abs/2310.11453 (BitNet)
- BitNet ARM regression pin-404980e: https://github.com/microsoft/BitNet/issues/556
- Bonsai Q1_0/Q2_0 upstream vs PTQ1_0/PQ2_0 fork-only:
  https://docs.prismml.com/run/llamacpp
- Qwen3-0.6B official GGUF + sampling/presence-penalty + think switching:
  https://huggingface.co/Qwen/Qwen3-0.6B-GGUF
- Qwen3 family/blog (Apr 2025, 0.6B–235B): https://github.com/qwenLM/qwen3
- Qwen3-0.6B quant ladders + sizes: https://huggingface.co/bartowski/Qwen_Qwen3-0.6B-GGUF
  (Q4_K_M 0.48 GB…), https://huggingface.co/tensorblock/Qwen_Qwen3-0.6B-GGUF
  (Q2_K 0.347 GB…Q8_0 0.805 GB), NANI-Nithin 30-file card (IQ4_XS→IQ2_M guidance)
- Qwen2.5-0.5B baseline (b3772 imatrix, Q2_K 339 MB / IQ2_M 329 MB):
  https://huggingface.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF
- Qwen3.5-0.8B (Feb 2026, hybrid DeltaNet, qwen35, 262K): https://apxml.com/models/qwen35-08b ;
  MTP merge + `--spec-type draft-mtp`: https://huggingface.co/unsloth/Qwen3.5-0.8B-MTP-GGUF/blob/main/README.md ;
  QAT Q4_0 8K-validated: https://huggingface.co/YoozLabs/Qwen3.5-0.8B-qat-GGUF ;
  Ollama 1.0 GB tag: https://ollama.com/library/qwen3.5/tags
- SmolLM3-3B (3B-only, 11.2T, NoPE, 128K) + official GGUF sizes (Q4_K_M 1.92 GB):
  https://huggingface.co/blog/smollm3 , https://huggingface.co/ggml-org/SmolLM3-3B-GGUF
- SmolLM2 family + Ollama tags + QuantFactory GGUF:
  https://ollama.com/library/smollm2 , https://huggingface.co/QuantFactory/SmolLM2-135M-GGUF
- Ollama GGUF import (FROM any GGUF; -q limited to K/legacy):

## Appendix A — measured baseline (2026-10-12, N1a)

Floor-flags `llama-bench` (v0.6.0, SSE2-only, same flags as the sidecar),
Ryzen 12-thread box pinned `-t 2` (this box far outruns Pentium D — treat as
ceiling-side for the floor class):

| Model | pp512 (tok/s) | tg128 (tok/s) |
|---|---|---|
| Qwen2.5-0.5B-Q2_K (415MB) | 7.83 ± 0.02 | 6.84 ± 0.01 |

Cross-checks server-side timings (8.5 prompt / 6.4 predicted @2t, same box).
N1b result (2026-10-12): Qwen3-0.6B-Q4_K_M @2t pp64 2.62 / tg16 2.34 —
~3x SLOWER than Qwen2.5-0.5B on the same path. NOT a speed upgrade;
quality edge unmeasurable here. Qwen2.5 stays default; Qwen3 opt-in only.
Remaining matrix: Qwen2.5-0.5B in IQ4_XS/IQ3_M (same-arch quant comparison).
- 2026-10-12 · IQ matrix @2t (pp64/tg16): IQ4_XS 7.12/6.07 (328MB),
  IQ3_M 5.95/5.22 (321MB) vs Q2_K 7.83/6.84 (415MB). Verdict: Q2_K stays
  speed default; IQ4_XS is the quality pick (better perplexity/byte,
  smaller file, ~10% slower). Quality axis per literature, unmeasured here.
  https://docs.ollama.com/import ; community IQ models proving runtime IQ support:
  https://ollama.com/vanilj/llama3.1-70b-iquants

## 7. Suggested next bench (2-thread box, then Snapdragon 439)

1. `llama-bench -t 2 -p 256 -n 128` on Qwen2.5-0.5B Q2_K (baseline) vs
   Qwen3-0.6B {Q4_K_M, IQ4_XS, IQ3_M, IQ2_M} — imatrix versions, `/no_think`,
   presence-penalty 1.5.
2. Same matrix with `-t 1` (oversubscription check per #5534 / #11247 class).
3. Winner goes to a quality gate (fixed prompt set, repetition/degradation
   check) before changing the default; IQ_K only enters via an ik_llama.cpp
   commit-pinned side build, never the Ollama path.

## Appendix B — SD680 on-device bench (2026-10-13, Termux native build)

Snapdragon 680 (Kryo 265), Termux Clang 21, v0.6.0, Qwen2.5-0.5B-Q2_K.
Feature probes: DOTPROD/SVE/MATMUL_INT8 failed, FMA ok (plain NEON).

| Threads | pp | tg |
|---|---|---|
| 2 (pp64/tg16) | 20.55 | 13.59 |
| 8 (pp512/tg128) | 47.18 | 15.42 |

Reads: 2t phone ≈ 2.6x the Ryzen dev box (SSE2 floor is the bottleneck
there, not the silicon). SD439 extrapolation (A53, weaker IPC): roughly
1/3 → mid-single-digit tok/s @8t — above the planning figure. Vehicle 2
(all-on-device experiment) PROCEEDS to the FastAPI+SurrealDB proot step;
companion-server+PWA remains the default product.

### Thermal/RAM verdict (operator report, same device/day)

`free -m`: 7618 total / 2905 available (+3105 buff/cache reclaimable),
5GB swap; model runs caused no UI smoothness defects, no abnormal heat,
no sustained-performance decay (6nm big.LITTLE behaves to spec). Verdict:
RAM and thermals clear vehicle 2 on SD680-class hardware with margin —
proot stack (~200–400MB API+DB) + 415MB model fits inside available RAM
without touching swap.
