# T1 — llama.cpp SSE2 Sidecar Build Recipe

Label: `wayfinder:research` (AFK). Parent: `../map.md`. Blocks: nothing.
Blocked by: nothing (frontier).

## Resolution (2026-10-11)

RESEARCHED — findings in `research-T1-findings.md` (same folder). Key deltas
vs starting assumptions: (1) pin llama.cpp revision ≥ 2025-04-21 (#12871 —
`GGML_NATIVE=OFF` alone insufficient before); (2) TinyLlama-1.1B effectively
OUT at 2GB default ctx — prefer Qwen2.5-0.5B-Q2_K primary, SmolLM2-360M
fallback (affects future model_router defaults); (3) binary size + tok/s are
measure-at-build TODOs, not facts; (4) proposed routing envs
`ALLOW_LLAMACPP_SSE2`/`LLAMACPP_SSE2_URL` fit the flag-split pattern.
Sidecar build is a later implementation slice, not this ticket.

## Measurements (2026-10-11, dev box Ryzen 12-thread, container pinned -t 2)

- Image `llamacpp-sse2:local` 146MB; binary 15MB stripped; `v0.6.0`
  (commit d812350 — pin verified); floor flags per Dockerfile.sse2.
- Serve Qwen2.5-0.5B-Q2_K (415MB) ctx 1024: **RSS 480MB**,
  **prompt 8.5 tok/s, predicted 6.4 tok/s** at 2 threads (server timings).
  Honest extrapolation to Pentium D class: divide several-fold → low
  single digits, consistent with research-T1 §5. Short answers fit;
  long generations do not.
- Env corrections: server binds 127.0.0.1 by default → compose sets
  `LLAMA_ARG_HOST=0.0.0.0`; thread knob is `LLAMA_ARG_THREADS` (and
  `LLAMA_ARG_THREADS_BATCH`), not `LLAMA_ARG_N_THREADS`.
- Environmental: this network 403s apt URLs with %-escapes — Dockerfile
  uses zero apt (prebaked toolchain + pip cmake + git clone). Main-image
  rebuilds may hit the same filter; stabilisation must use cached layers.
- Routing hook (`ALLOW_LLAMACPP_SSE2`) still unwired — next slice.

## Question

What is the exact, reproducible recipe for a no-AVX `llama.cpp` inference
sidecar that runs Qwen2.5-0.5B / SmolLM2-135M-360M / TinyLlama-1.1B quants in
the Docker-first stack on a Pentium D / Athlon 64 (SSE2/SSE3, 2 threads, 2GB)?

Resolve against primary sources (llama.cpp PR #12871 x64-base-ABI / SSE4.2
variants, NeuralXP `-march=pentium-m -mno-sse3 -DGGML_BMI2=OFF
-DGGML_CPU_ALL_VARIANTS=OFF` precedent, `GGML_NATIVE=OFF` portable builds).
Record: base image + toolchain, CMake flags, resulting binary size, cold-start
RAM for 0.5B Q2_K/Q3_K_M, tokens/sec expectation, and the compose wiring
(opt-in profile, env `ALLOW_LLAMACPP_SSE2`, routing hook in
`open_notebook/ai/provision.py` behind `FORCE_MINI`). Constraints:
`OPEN_NOTEBOOK_WORKER_MAX_TASKS=1` untouched; host Ollama remains default;
worker heartbeats unaffected (sidecar is HTTP-called, never in-process torch).
