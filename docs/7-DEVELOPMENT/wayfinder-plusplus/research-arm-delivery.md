# ARM Delivery Research — running the open-notebook stack on Android phones

Date: 2026-10-12. Scope: read-only research for open-notebook low-end tier.
Target floor: Snapdragon 439 phones (8× Cortex-A53, 3 GB RAM, 32 GB eMMC);
test device Snapdragon 680 / 8 GB. Current stack (per repo `docker-compose.yml`):
Python FastAPI (`open_notebook` service, ports 8502/5055) + SurrealDB v2
(RocksDB backend) + Next.js 16 frontend (`frontend/`, `output: "standalone"`,
`/api/*` rewrites proxy to FastAPI on :5055) + host Ollama + optional
whisper.cpp / piper sidecars. All claims cite primary sources in §8.

## 1. Ranked delivery vehicles

| Rank | Vehicle | Verdict | Rationale |
|---|---|---|---|
| 1 | **Companion-server split** — full Docker Compose stack stays on a cheap LAN box (x86 mini-PC / existing host); phone runs the Next.js frontend as an installable **PWA** pointed at the server | **ADOPT** | Zero Android fragility: SurrealDB/RocksDB, Ollama, whisper/piper, `rewrites()` proxy all keep working unchanged. Phone needs only a browser. Matches PWA guidance (manifest + HTTPS + service worker, §5) and the existing `output: "standalone"` frontend, which already proxies `/api/*` server-side. Only vehicle that delivers the *whole* product on day one. |
| 2 | **All-on-device via Termux + proot-distro Ubuntu, no Docker** — llama.cpp native binary + FastAPI/uvicorn + SurrealDB `linux-arm64` binary inside an Ubuntu proot; UI as locally-served PWA or the same remote PWA pointed at `localhost` | **FLAG-GATE** (expert build, bench on SD439 first) | Every component has a viable path (§2–§4), but it is 4 fragile pieces (proot I/O, bionic/glibc split, 3 GB RAM, eMMC speed) multiplied together. Right for offline-only demos and the SD680 test device; not the default until a full-stack bench passes on the real SD439 floor. |
| 3 | **Native inference app + remote API/DB** — llama.cpp `examples/llama.android` (or OllamaServer / MLC-style app) for on-device chat, SurrealDB/FastAPI remain server-side | **FLAG-GATE** (inference-only win) | Best inference UX and battery story (proper NDK build, KleidiAI dispatch, no proot overhead), but it splits the codebase (Kotlin/JNI shell around the same GGUFs) and answers only the inference slice — notes/cards/FSRS/SurrealDB still need vehicle 1. Pursue only if on-device chat must work with zero server. |
| 4 | **Docker-on-Android** (dockerd in Termux / proot / rooted kernel) | **SKIP** | No official path; daemon needs kernel namespaces, cgroups and overlay mounts Android kernels withhold, and proot cannot emulate kernel features (§3). Community successes require root + custom kernels — unacceptable as a product delivery vehicle. |

Bottom line: **ship vehicle 1 (PWA client + server split) as the product; use
vehicle 2 as the offline experiment on the SD680; do not invest in vehicle 4.**

## 2. Per-component feasibility (on-device vs companion-server split)

| Component | On-device (Termux/proot) | Split (recommended) | Notes |
|---|---|---|---|
| FastAPI API (`api/`, uvicorn) | **VIABLE with caveats** | Runs on server, unchanged | `pkg install python` is first-class on Termux (wiki); FastAPI installs, but `uvicorn[standard]` (httptools/uvloop compiled deps) has a history of Termux build failures — pin pure-Python `uvicorn` (h11) or hypercorn, and run inside the Ubuntu proot (glibc) if wheels misbehave. Single worker (`WORKER_MAX_TASKS=1` already), bind `127.0.0.1`, expect ~200–400 MB RSS with lazy imports. |
| SurrealDB v2 (RocksDB) | **FLAG-GATE** | Runs on server, unchanged | Releases ship `linux-arm64.tgz` and the install script detects platform/CPU — but those binaries target glibc Linux, **not** Android bionic, so run them inside the proot-distro Ubuntu userland, never bare Termux. Risks: RocksDB write amplification on slow eMMC, file-locking under proot, and RAM (keep one DB, SurrealKV/file-backed only if RocksDB proves too heavy — file-backed mode is documented). Pre-create DB on server, ship a seed snapshot; treat on-device DB as cache, not source of truth. |
| Next.js UI (`frontend/`) | **ADOPT as PWA client** (not as on-device server) | Served from server; phone installs it | Do NOT run `next start` on the phone (Node server + 100 MB-proxy stack for zero benefit). Instead build once on the server and install on the phone as a PWA (§5). Caveat: current `next.config.ts` uses `output: "standalone"` + `rewrites()` + `redirects()`, and **all three are unsupported under `output: "export"`** static export — a fully-offline PWA needs the rewrite proxy removed (client fetches FastAPI base URL directly) and the redirect replaced. Keep `standalone` for vehicle 1; do the `export` migration only for vehicle 2. |
| LLM inference (llama.cpp sidecar / Ollama host) | **llama.cpp native: ADOPT; Ollama daemon: SKIP on-device** | Ollama stays on server | llama.cpp has a documented portable `arm64-v8a` NDK build and a Termux path (§6). Ollama has no official Android support (upstream issue open, community Termux forks only) and its daemon + model store assume a real Linux host — keep Ollama server-side and speak to it from the phone over the network. |
| whisper.cpp / piper sidecars | **SKIP on-device** | Server-side profiles only | Both are in `media` compose profiles already; VibeASR.cpp (BitNet-family, NEON) is the only on-device STT worth re-evaluating later, and only on the SD680 first. |

## 3. Docker-on-Android — why it usually fails (SKIP)

1. **The daemon needs kernel features Android withholds.** dockerd must create
   namespaces, write cgroups and mount overlayfs; stock Android kernels (and the
   Termux sandbox, no root) deny all three. Community guides confirm: "cannot run
   without root because the daemon has to create kernel namespaces, write cgroups
   and mount" and overlay fails inside the Termux prefix.
2. **proot is a userland syscall emulator, not a kernel.** proot-distro runs full
   Linux *userlands* (Ubuntu/Debian/OCI images) without root — which is exactly
   what vehicle 2 needs — but anything requiring real namespaces, cgroups or
   seccomp (dockerd, `docker build` kernel steps) fails or behaves subtly
   differently. The proot-distro docs themselves limit `RUN`-style isolation
   expectations to "no PID, network, or IPC isolation, no cgroups, no seccomp".
3. **"Successes" need root + custom kernels,** i.e. a different device than the
   one we ship to. Not a product vehicle; useful only as a lab curiosity.

Consequence: vehicle 2 reuses the *images/userlands* (Ubuntu 24.04 via
`proot-distro install ubuntu:24.04`) but starts processes directly
(`proot-distro login/run`), never via dockerd. Compose files become checklists,
not runtimes, on-device.

## 4. Termux / proot-distro viability (vehicle 2 substrate)

- **Termux native layer: GOOD for Python + Node, BAD for glibc binaries.**
  Termux is a rolling-release Android terminal + package collection (`pkg install
  python`, `pkg install nodejs` for current/LTS Node). FastAPI + a pure-Python
  ASGI server and the Node runtime run here. But Termux userland is bionic-based,
  so prebuilt `linux-arm64` binaries (SurrealDB, many wheels with native
  extensions) may refuse to run — that is what the Ubuntu proot is for.
- **proot-distro Ubuntu layer: GOOD for server binaries.** `proot-distro`
  (Termux `pkg`, or PyPI) pulls OCI images straight from registries with no
  daemon and gives a glibc Ubuntu 24.04 userland: `proot-distro install
  ubuntu:24.04 && proot-distro login ubuntu`. Run SurrealDB's install script and
  `pip install` the API requirements here. Known costs: syscall-translation
  overhead on file-heavy workloads (RocksDB!), no nested proot, background
  processes get killed by Android unless the session holds a wake lock /
  foreground notification — plan `proot-distro run … --detach` + Termux:Boot or
  a foreground service for anything long-lived.
- **Practical split inside vehicle 2:** Termux-native = llama.cpp binary +
  PWA static files (serve via any static server); Ubuntu proot = FastAPI +
  SurrealDB. Bind-mount the model dir into the proot (`--bind`) so GGUFs live
  once on disk.

## 5. PWA packaging of the Next.js frontend (phone UI for vehicles 1 and 2)

Next.js documents the PWA recipe directly: an App-Router `app/manifest.ts`
(`name`, `short_name`, `display: "standalone"`, 192/512 px icons in `public/`),
a `lib/service-worker.js` (push + `notificationclick`), install-prompt handling
(`beforeinstallprompt` deliberately not recommended — rely on the browser
prompt), and HTTPS + valid manifest as the installability bar. Offline caching
is explicitly delegated: use the experimental `useOffline` hook/config for
connectivity-aware UI, or a service-worker toolkit (Serwist, with
Next.js/Turbopack examples) for full asset caching.

What this means per vehicle:

- **Vehicle 1 (recommended): keep `output: "standalone"`.** The server renders
  and the existing `/api/*` rewrite proxy keeps working (rewrites need a Node
  server). Add `manifest.ts` + icons + service worker + security headers per
  the PWA guide; no frontend logic changes required. Push-notification server
  actions from the guide are optional — open-notebook needs installability and
  offline *reading* first, not push.
- **Vehicle 2 (offline phone): migrate a PWA slice to `output: "export"`.**
  Static export (`out/` HTML/CSS/JS servable from any static server) supports
  Server Components (pre-rendered at build) and client-side fetching (SWR-style),
  but **forbids** `rewrites`, `redirects`, `headers`, cookies, ISR, default
  image optimization, Server Actions and dynamic routes without
  `generateStaticParams()`. Concretely: delete the `/api/*` rewrite (client
  calls `http://127.0.0.1:5055/api/*` directly), drop/replace the
  `/settings/api-keys → /settings/models` redirect, and use a custom image
  loader or plain `<img>`. The repo grep confirms no `use server` / route
  handlers in `frontend/src` today, so the migration surface is exactly those
  three config items — small but must be a separate build flavor, not a flag
  flip on the main config.

## 6. llama.cpp on Android (inference engine for vehicles 2 and 3)

- **Official build path exists (primary doc `docs/build.md`).** Portable
  Android `arm64-v8a` NDK cross-build from the llama.cpp source dir with
  `ANDROID_ABI=arm64-v8a`, `ANDROID_PLATFORM=android-28`,
  `GGML_CPU_KLEIDIAI=ON`, `GGML_NATIVE=OFF` (host CPU ≠ target),
  `GGML_OPENMP=OFF`, `GGML_LLAMAFILE=OFF`, `LLAMA_OPENSSL=OFF`. The
  `examples/llama.android` Studio project enables KleidiAI for `arm64-v8a`
  automatically; Termux-native builds (`cmake -B build && cmake --build`) are
  the documented no-root alternative.
- **CPU reality on Snapdragon 439: 8× Cortex-A53, ARMv8-A, plain NEON.**
  In-order dual-issue cores, mandatory NEON + VFPv4 — but **no dotprod, no i8mm,
  no SVE** (those arrived in later cores; A53 is Arm's efficiency baseline).
  KleidiAI dispatch is runtime-detected with automatic fallback to compatible
  kernels, so a portable build *runs* on A53 but gets the generic-NEON path,
  not the fast dotprod/i8mm/SVE kernels — budget "slow but correct"
  (single-digit tok/s at 0.5–0.6B is the planning figure; must bench
  `llama-bench` on the actual SD439, cf. sibling `research-cpu-infer.md` §2.4).
  The SD680 test device (Kryo 265 = A73-class + A53, Adreno 610) is meaningfully
  faster — always report which SoC a bench number came from.
- **GPU offload: don't count on it for SD439.** The OpenCL backend targets
  recent Adreno; Adreno 505-era OpenCL drivers on budget phones are the
  historically flaky path (community Vulkan/OpenCL Android builds report more
  debugging than speedup on old SoCs). Plan CPU-only for the floor; treat any
  Adreno 610 (SD680) offload as a bench-gated bonus.
- **mmap vs slow eMMC: keep mmap ON, keep models on internal storage.**
  llama.cpp loads weights via `mmap()` (~100× faster loads, half the RAM vs
  stdio reads) — disabling it (`--no-mmap`) forces full reads through page
  cache and *increases* RAM pressure, exactly wrong on 3 GB. Known Termux
  pun: direct-I/O default bugs mean pass `--mmap` explicitly if loads crawl.
  eMMC random-read is the bottleneck, so: store active GGUFs on **internal**
  flash (never execute/mmap hot weights off a FAT32 SD card — noexec mounts and
  4 GB file caps bite), keep `--ctx-size` small (512–1024), threads ≈ 4
  (little-core oversubscription hurts), and consider zram relief (documented
  `kswapd0`-at-100% failure mode when RAM + mmap pressure collide).
- **Threading flags for the NDK build:** ship `GGML_OPENMP=OFF` per the
  documented portable recipe; tune `-t` at runtime instead.

## 7. BitNet / ternary ARM state (experimental only — consistent with sibling research-cpu-infer.md §3)

- **Official support matrix (BitNet README, primary):** the 2.4B
  `BitNet-b1.58-2B-4T` model lists CPU kernels I2_S ✅ + TL1 ✅ on **ARM** (TL2 ❌
  on ARM); community 0.7B/1–3B ternaries likewise carry ARM I2_S/TL1 rows. So a
  ternary chat model *can* run on ARMv8 — but note the embedding models
  (0.6B/270M) are **x86-only (ARM ❌)**, ruling out a BitNet embedding path
  on-device.
- **Costs that keep it gated:** separate runtime + `ggml-model-i2_s.gguf`
  conversion (`setup_env.py -md <dir> -q i2_s`), toolchain floor
  (python ≥ 3.10, cmake ≥ 3.22, **clang ≥ 18** — heavier than the llama.cpp
  gcc/NDK build), no Ollama interop, and documented ARM fragility (issue #556:
  ARM regressions producing garbage output, fixed only by commit-pinning plus
  manual chrono includes and hand-set i8mm/dotprod flags — flags an SD439
  *lacks*, so the generic NEON fallback is the only option). Claimed speedups
  (up to ~5× ARM, −55–70% energy) are measured on modern cores, not A53.
- **Verdict: SKIP by default; FLAG-GATE the same experiment as the sibling
  doc** — pin BitNet by commit, convert `BitNet-b1.58-2B-4T` with `-q i2_s`,
  bench `e2e_benchmark.py -p 256 -n 128 -t 4~8` against Qwen3-0.6B IQ4_XS on
  the SD680 first, SD439 second. Never the default runtime before that bench.

## 8. Model distribution plan for 32 GB eMMC

### 8.1 Storage budget (planning figures — measure on the real device)

32 GB eMMC phones typically yield ~22–24 GB user-visible, of which the system +
preinstalled apps + user apps leave roughly **8–12 GB free** on a used device.
Vehicle 1 needs ~0 MB on the phone (PWA cache only, tens of MB). Vehicle 2
budget:

| Item | Size | Location |
|---|---|---|
| Termux app + bootstrap | ~0.5 GB | internal (private app dir) |
| Ubuntu 24.04 proot + Python/Node toolchain | ~2–4 GB | internal |
| SurrealDB binary + DB files | ~0.1–0.5 GB | internal (RocksDB wants real fsync) |
| Chat GGUF: Qwen3-0.6B IQ4_XS (~0.44 GB) or IQ2_M (~0.32 GB) | ~0.3–0.5 GB | **internal** (hot, mmap) |
| Embed GGUF: nomic-class ≤ 0.3 GB (quantized) | ~0.2–0.3 GB | internal (hot) or SD |
| STT/TTS voices (piper per-voice ~0.1 GB) | as-needed | SD card, fetch on demand |
| PWA static export (`out/`) | tens of MB | anywhere |
| **Vehicle-2 total** | **~3.5–6 GB** | fits 8–12 GB free with margin |

One chat + one embed model on internal flash; everything else streams from SD
or server. Never ship Q8_0/F16 variants to the phone (0.6–0.8 GB each for
0.6B — pure waste on eMMC).

### 8.2 SD card rules (Android storage reality)

- Prefer **portable storage** (not adoptable): adoptable storage encrypts and
  merges the card with internal (GPT-based, ~transparent), but the card becomes
  device-locked and its speed caps the whole pool — wrong for removable model
  packs. Portable FAT32/exFAT cards are readable everywhere but carry a **4 GB
  FAT32 per-file cap** (single GGUFs here are < 1 GB, fine) and **noexec**
  mounts — data files are OK, executables/binaries must live in Termux's
  private dir.
- Termux reaches shared storage at `/sdcard` (needs `termux-setup-storage`
  grant); scoped-storage restrictions apply to app-private dirs, so keep the
  model depot in a stable shared path (e.g. `/sdcard/open-notebook/models/`)
  and `--bind` it into the proot / symlink hot models to internal.
- Update flow: download to SD → checksum → promote hot files to internal.
  Keep at least one previous-known-good GGUF on SD as rollback.

### 8.3 Resumable, cheap-bandwidth downloads (primary-tooling plan)

- Ship downloads via tools that resume: `aria2c -c` (multi-connection,
  metalink-capable), `wget -c`, or `curl -C -`. For Hugging Face sources use
  the `huggingface_hub` snapshot/download helpers (cached, resumable,
  per-file) rather than browser downloads — model cards (Qwen3-0.6B-GGUF,
  bartowski/TensorBlock ladders) are the canonical per-quant URLs.
- Provide a tiny on-device fetch script (`fetch-model.sh <quant>`): tries
  `hf download` → falls back to `aria2c -c -x4 <url>` → `sha256sum -c`.
  Chunk choice: IQ4_XS default, IQ3_M tight-RAM fallback, IQ2_M floor
  (per sibling `research-cpu-infer.md` §2 sizing: ~0.44 / ~0.39 / ~0.32 GB).
- First-boot UX: vehicle 1 needs no download; vehicle 2 downloads exactly one
  chat quant on setup (default IQ4_XS), offers embed + voices as opt-in packs.
  Show bytes + resume state; never re-download a checksum-verified file.

## 9. Recommended next steps (bench-gated)

1. **Vehicle 1 now:** add `app/manifest.ts` + icons + service worker to
   `frontend/` per the Next.js PWA guide; serve over HTTPS; test
   Add-to-Home-Screen against the live server from the SD439 Chrome. No other
   stack changes.
2. **Vehicle 2 spike (SD680 first):** Termux + `proot-distro install
   ubuntu:24.04`; SurrealDB `linux-arm64` binary in proot; pure-Python uvicorn
   FastAPI; NDK-built `llama-cli` (`arm64-v8a`, KleidiAI on); run
   `llama-bench -p 256 -n 128` for Qwen3-0.6B {IQ4_XS, IQ3_M, IQ2_M} and record
   PP/TG + peak RSS. Promote to SD439 only on pass.
3. **Static-export flavor** of the frontend (rewrite/redirect removal) only
   after step 2 passes — it is vehicle-2-only work.
4. **BitNet bench** dead last, gated identically (see §7).

## 10. Sources (primary, per claim)

- llama.cpp Android/KleidiAI NDK build recipe (ABI `arm64-v8a`,
  `ANDROID_PLATFORM=android-28`, `GGML_NATIVE=OFF`, `GGML_OPENMP=OFF`,
  runtime KleidiAI dispatch + fallback, OpenCL-on-Adreno section):
  https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md
- llama.cpp Android NEON/i8mm detection + Termux/DirectIO-mmap issues:
  https://github.com/ggml-org/llama.cpp/issues/10662 ,
  https://github.com/ggml-org/llama.cpp/issues/18804 ;
  mmap design (mmap vs stdio, ~100× load): https://justine.lol/mmap/ ;
  zram/`kswapd0` on-device pressure discussion:
  https://www.reddit.com/r/LocalLLaMA/comments/1ebnkds/llamacpp_android_users_now_benefit_from_faster/
- BitNet README (I2_S/TL1 ARM matrix, embeddings x86-only, clang ≥ 18,
  `setup_env.py -q i2_s`, 1.37–5.07× ARM / −55–70% energy):
  https://github.com/microsoft/BitNet ; tech report:
  https://arxiv.org/abs/2410.16144 ; ARM regression pin-`404980e`:
  https://github.com/microsoft/BitNet/issues/556
- proot-distro (OCI-image userlands without root/daemon, no-PID/cgroup/seccomp
  limits, `install`/`login`/`run --detach`, `--bind`):
  https://github.com/termux/proot-distro
- Docker-on-Android failure analysis (namespaces/cgroups/overlay need root):
  https://cosyra.com/guides/run-docker-on-termux.html ;
  https://www.reddit.com/r/termux/comments/1t40iuj/i_wrote_a_container_engine_from_scratch_in_go/ ;
  rooted-kernel-only path: https://ivonblog.com/en-us/posts/sony-xperia-5-ii-docker-kernel/
- Termux Python (`pkg install python`) and Node.js (`pkg install nodejs`,
  current/LTS): https://wiki.termux.com/wiki/Python ,
  https://wiki.termux.com/index.php?title=Node.js ;
  uvicorn-on-Termux build friction:
  https://github.com/Kludex/uvicorn/issues/1249 ,
  https://stackoverflow.com/questions/69965946/problem-installing-uvicorn-in-termux-android
- Ollama Android status (no official support; Termux forks only):
  https://github.com/ollama/ollama/issues/721 ;
  community Termux reports:
  https://www.reddit.com/r/termux/comments/1aii9xu/ollama_on_android_gpu/ ,
  https://news.ycombinator.com/item?id=44525807
- SurrealDB Linux install (script detects platform/CPU), releases with
  `linux-arm64.tgz`, file-backed RocksDB/SurrealKV single-node mode:
  https://surrealdb.com/docs/running/installation/linux ,
  https://github.com/surrealdb/surrealdb/releases ,
  https://surrealdb.com/docs/running/file-backed
- Next.js PWA guide (manifest, service worker, installability, no
  `beforeinstallprompt` recommendation): https://nextjs.org/docs/app/guides/progressive-web-apps ;
  static export (`output: "export"`, supported/unsupported matrix — rewrites,
  redirects, headers, Server Actions unsupported):
  https://nextjs.org/docs/app/guides/static-exports ;
  offline extension points (`useOffline`, Serwist) in the same PWA guide
- SoC facts: Snapdragon 439 (8× Cortex-A53, Adreno 505):
  https://www.notebookcheck.net/Qualcomm-Snapdragon-439-SoC-Benchmarks-and-Specs.423800.0.html ,
  https://www.qualcomm.com/news/onq/2018/06/introducing-snapdragon-632-439-and-429-enhanced-mobile-experiences-superior ;
  Cortex-A53 (in-order, NEON/VFPv4 mandatory, no dotprod/i8mm/SVE):
  https://www.arm.com/products/silicon-ip-cpu/cortex-a/cortex-a53 ,
  https://en.wikipedia.org/wiki/ARM_Cortex-A53 ,
  https://chipsandcheese.com/p/arms-cortex-a53-tiny-but-important
- Android storage (adoptable vs portable, GPT, app-data rules):
  https://source.android.com/docs/core/storage/adoptable ;
  microSD reality on modern Android:
  https://www.androidauthority.com/using-microsd-card-android-3362538/
- Resumable transfer tooling: aria2 (`-c` continue):
  https://aria2.github.io/manual/en/html/aria2c.html ;
  huggingface_hub download guide (cache + resume):
  https://huggingface.co/docs/huggingface_hub/guides/download ;
  model sources: https://huggingface.co/Qwen/Qwen3-0.6B-GGUF ,
  https://huggingface.co/bartowski/Qwen_Qwen3-0.6B-GGUF ,
  https://huggingface.co/tensorblock/Qwen_Qwen3-0.6B-GGUF
- Repo-local evidence: `docker-compose.yml` (SurrealDB v2 + RocksDB,
  FastAPI :5055/:8502, Ollama host, `media` profiles), `frontend/package.json`
  (Next 16, React 19), `frontend/next.config.ts` (`output: "standalone"` +
  `/api/*` rewrites + redirect — the static-export blockers), sibling
  `research-cpu-infer.md` (§2 quant sizing, §3 BitNet gate, §7 bench protocol).
