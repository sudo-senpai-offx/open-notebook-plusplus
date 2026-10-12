# Speech on low-end CPU research (read-only)

Date: 2026-10-12. Sources are PRIMARY unless marked `(secondary)`.
Scope: whisper.cpp STT model ladder for CPU-only weak hosts, the
`ggml-large-v3-turbo-q5_0` default in `docker-compose.yml` (`whisper` service,
`profiles: ["media"]`), Piper TTS voice tiers (`en_US-amy-medium` etc. in
`~/piper_models`), and VAD segmentation to cut STT cost. No code changed.

## 0. TL;DR

- **large-v3-turbo is NOT faster than base.en on a weak CPU.** Its encoder is
  architecturally identical to large-v3 (32 layers x d=1280, 128 mels); only
  the decoder was cut 32 -> 4 layers. On CPU the encoder dominates, so turbo
  costs ~large-class encoder time per 30 s chunk. Base.en's encoder is
  6 layers x d=512 / 80 mels — roughly 30x fewer encoder FLOPs. Details in
  section 2.
- Recommended ladder (disk / runtime RAM are primary numbers, section 1):
  `tiny.en-q5_1` (31 MiB) -> `base.en-q5_1` (57 MiB) -> `small.en-q5_1`
  (181 MiB) -> `medium-q5_0` (514 MiB). Keep `large-v3-turbo-q5_0` (547 MiB
  disk, ~770 MB runtime) as an opt-in quality tier, not the default.
- VAD (Silero ggml, 864 KB) skips silence before Whisper sees it; savings are
  proportional to the silence fraction and VAD overhead is ~1 ms per 30 ms
  chunk on one thread. It is the cheapest win for long/podcast audio.
- Piper tiers are real and measurable: `low`/`x_low` voices render at
  16 kHz, `medium`/`high` at 22.05 kHz (voice config JSONs); local
  measurement: `medium` .onnx = 63.2 MB, `high` = 114.2 MB. `en_US-amy` ships
  only `low`+`medium`, so amy-medium is already the best amy available.
- Repo-state warning (local observation 2026-10-12): the checked-in
  `whisper_models/ggml-large-v3-turbo-q5_0.bin` is a **29-byte placeholder**
  whose content is the text `Invalid username or password.` — re-download
  before any benchmark.

## 1. whisper.cpp model ladder: sizes and RAM (primary)

Disk sizes, `models/README.md`
(https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md and
mirror https://huggingface.co/ggerganov/whisper.cpp):

| Model | Disk | Disk q5 | Runtime RAM (full precision) |
| --- | --- | --- | --- |
| tiny / tiny.en | 75 MiB | tiny-q5_1 31 MiB | ~273 MB |
| base / base.en | 142 MiB | base-q5_1 57 MiB | ~388 MB |
| small / small.en | 466 MiB | small-q5_1 181 MiB | ~852 MB |
| medium / medium.en | 1.5 GiB | medium-q5_0 514 MiB | ~2.1 GB |
| large-v1/v2/v3 | 2.9 GiB | large-v3-q5_0 1.1 GiB | ~3.9 GB (large) |
| large-v3-turbo | 1.5 GiB | **large-v3-turbo-q5_0 547 MiB** | (see measured log below) |

- Runtime RAM column is from the main README memory table
  (https://github.com/ggml-org/whisper.cpp README "Memory usage").
- Quantized runtime for turbo-q5_0, from a primary bench log in
  https://github.com/ggml-org/whisper.cpp/issues/89 (2025-10-11 report,
  Xeon Silver 4214, v1.8.0): `model size = 573.40 MB` + `kv self 10.49 MB` +
  `kv cross 31.46 MB` + compute buffers ~36 + 55 + 9 + 99 MB = **~770 MB
  resident** before audio buffers. Same log for full `base.en`: `CPU total
  size = 147.37 MB` + kv/buffers ~150 MB = **~300 MB resident**
  (https://github.com/ggml-org/whisper.cpp/issues/89, 2025-12-03 report,
  Ryzen 7 5700X).
- OpenAI's upstream table
  (https://github.com/openai/whisper README "Available models and
  languages"): params tiny 39 M, base 74 M, small 244 M, medium 769 M,
  large 1550 M, **turbo 809 M**; relative GPU (A100) speed vs large: tiny
  ~10x, base ~7x, small ~4x, medium ~2x, **turbo ~8x**. Note: A100 numbers,
  not CPU — do not use them for low-end sizing (section 2). Also per that
  table, `.en` models "tend to perform better, especially for tiny.en and
  base.en".

### Recommended ladder per free-RAM tier (low-end CPU-only)

| Free RAM | STT model | Why |
| --- | --- | --- |
| <= 512 MB | `tiny.en-q5_1` (31 MiB disk) | Only thing that fits with headroom; RTF still < 1 on Pi-class (section 3) |
| ~1 GB | `base.en-q5_1` (57 MiB) or full `base.en` (142 MiB) | Best quality that stays comfortably resident; `.en` preferred per OpenAI note above |
| ~2 GB | `small.en-q5_1` (181 MiB) or full `small.en` (466 MiB) | Needs ~852 MB runtime full-precision; the in-repo `docker-compose-full-local.yml` example already calls small "balanced, ~500MB" (`examples/docker-compose-full-local.yml` lines ~146-167) |
| >= 3 GB | `medium-q5_0` (514 MiB) | 1.5 GiB full model needs ~2.1 GB runtime — quantized only on low-end |
| Opt-in only | `large-v3-turbo-q5_0` (547 MiB, ~770 MB runtime) | Large-class quality where affordable; never the default on <= 2 threads (section 2) |

## 2. Is large-v3-turbo faster than base.en on 2 threads? No.

Three primary architecture facts, then the consequence:

1. **Turbo's encoder = large-v3's encoder.** HF config
   https://huggingface.co/openai/whisper-large-v3-turbo/resolve/main/config.json:
   `encoder_layers 32, d_model 1280, encoder_attention_heads 20,
   num_mel_bins 128`, `decoder_layers 4`. The model card
   (https://huggingface.co/openai/whisper-large-v3-turbo) states it
   verbatim: "it's the exact same model [as large-v3], except that the
   number of decoding layers have reduced from 32 to 4", "way faster, at the
   expense of a minor quality degradation".
2. **Base's encoder is tiny by comparison.** HF config
   https://huggingface.co/openai/whisper-base/resolve/main/config.json:
   `encoder_layers 6, d_model 512, encoder_attention_heads 8, num_mel_bins
   80`, decoder 6 layers. (tiny: 4 x d=384 / 80 mels; small: 12 x d=768 /
   80 mels — https://huggingface.co/openai/whisper-tiny and
   https://huggingface.co/openai/whisper-small `config.json`.)
3. **On CPU the encoder dominates.** Same Xeon turbo-q5_0 log as section 1
   (https://github.com/ggml-org/whisper.cpp/issues/89): `encode time =
   14003 ms / 1 run` vs `decode time = 2336 ms / 256 runs`. The 4-layer
   decoder saving is real but it applies to the smaller term.

Back-of-envelope (arithmetic from facts 1-3, **extrapolation — measure it**):
encoder matmul cost scales ~ layers x d_model^2, i.e.
(32/6) x (1280/512)^2 ~= 5.3 x 6.25 ~= **~33x** more encoder FLOPs for turbo
than base per 30 s window (plus 128 vs 80 mel bins at the input conv only).
So on 2 weak threads turbo is expected ~10-30x *slower* per chunk than
base.en, not faster, and not "better quality per second" either — for a
fixed CPU-time budget base/small win; turbo only wins on *absolute* quality
if you can afford large-class encoder time plus ~770 MB RAM.

- OpenAI's "~8x" turbo figure is explicitly "transcribing English speech on
  a A100" (https://github.com/openai/whisper README) — GPU, decoder-bound,
  irrelevant to 2-thread CPU sizing. Do not cite it as a CPU claim.
- Practical reading: if base.en transcribes a 30 s chunk in ~2.5 s on 2
  threads of a modern mobile CPU (measured 2534 ms, section 3), turbo on the
  same 2 threads is extrapolated to ~25-60 s+ per chunk. On Pi-4-class
  hardware (base encoder already ~30 s, section 3) turbo is likely
  minutes-per-chunk — verify before ever making it the default.

## 3. CPU benchmarks on weak hardware (primary: issue #89 + bench.cpp)

What `whisper-bench` "Enc." means (primary):
`examples/bench/bench.cpp`
(https://github.com/ggml-org/whisper.cpp/blob/master/examples/bench/bench.cpp)
`whisper_bench_full` runs **1x `whisper_encode`** over a full-length mel
context (set via `whisper_set_mel(ctx, nullptr, 0, n_mels)`), then 256x
single-token decode + 64x 5-token batched decode + 16x 256-token prompt
passes. So **Enc ms ~= encoder time for one 30 s window**; realtime-factor
(RTF) per chunk ~= Enc_ms / 30000. It is encoder-centric by design.

Weak-hardware Enc numbers, all from
https://github.com/ggml-org/whisper.cpp/issues/89 (pinned benchmark thread):

| CPU | Threads | tiny.en | base.en | small.en | medium | Implied RTF (Enc/30 s) |
| --- | --- | --- | --- | --- | --- | --- |
| Raspberry Pi 4 (NEON) | 4 | 13839 ms (tiny) | 30552 ms (base) | — | — | tiny ~0.46x (2.2x slower than realtime); base ~1.0x encoder-only |
| i7-4790K (2014 desktop) | 8 | 783 ms | 1746 ms | — | — | base ~17x |
| Ryzen 5 4500U (6C/6T laptop) | 6 | 672 ms | 1496 ms | 6371 ms | 19673 ms | base ~20x; small ~4.7x; medium ~1.5x |
| i7-11800H (WSL2) | **2** | 1088 ms | 2534 ms | 8714 ms | — (large 57109 ms) | base ~12x; small ~3.4x; large ~0.5x |
| i5-1135G7 (ultrabook) | 8 | — | — | — | 24890 ms (medium) | medium ~1.2x |
| Xeon Silver 4214 (server) | 22 | — | — | — | turbo-q5_0 ~14000 ms | turbo ~2.1x encoder-only |
| M1 Ultra + CoreML (contrast, not CPU) | 8 | 19 ms | 34 ms | 115 ms | turbo-q8_0 618 ms | N/A (ANE offload) |

Caveats, all primary-sourced:

- **bench != end-to-end transcription.** A detailed breakdown in the same
  thread (2025-12-29 report) shows `whisper-bench` is a synthetic
  kernel benchmark while `whisper-cli` adds audio decode, mel extraction,
  30 s segmentation, and autoregressive beam-search decode that scales with
  output tokens. Always confirm with real `whisper-cli` wall time
  (https://github.com/ggml-org/whisper.cpp/issues/89).
- **SIMD build flags can dominate model choice.** A Ryzen 7 5700X report in
  the same thread shows base.en Enc = 7786 ms because the binary exposed
  only SSE3/AVX (no AVX2/FMA), vs ~880 ms for an AVX2 build on the older
  Ryzen 9 3900X. Ship AVX2 (or version-appropriate) builds before
  upsizing models (https://github.com/ggml-org/whisper.cpp/issues/89).
- Numbers span 2022-2025 commits; treat as order-of-magnitude guides and
  re-run `whisper-bench -m <model> -t 2` on the target host (section 6).

Expected realtime factors on a 2-thread weak CPU (extrapolated, to verify):
base.en ~5-12x, small.en ~1.5-3.5x, medium sub-realtime ~1x, turbo
sub-realtime (encoder ~= large). End-to-end RTF will be lower than
encoder-only RTF once decode + beam search are included.

## 4. Piper voice tiers: sizes and quality (primary)

Tier availability (https://github.com/rhasspy/piper/blob/master/VOICES.md):
`en_US-amy` ships **low + medium only** (no high); `en_US-lessac` and
`en_US-ryan` ship low + medium + high; `en_US-ljspeech` ships medium +
high. Tiers range x_low -> low -> medium -> high.

Sample rates from the voice config JSONs (primary, `audio.sample_rate` +
`audio.quality` fields):

| Voice file | quality | sample_rate |
| --- | --- | --- |
| `en_US-amy-medium.onnx.json` (local `~/piper_models`, = HF v1.0.0) | medium | 22050 |
| `en_US-amy-low.onnx.json` (https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/low/en_US-amy-low.onnx.json?download=true) | low | 16000 |
| `en_US-lessac-high.onnx.json` (https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/high/en_US-lessac-high.onnx.json?download=true) | high | 22050 |
| `en_US-ljspeech-high.onnx.json` (local) | high | 22050 |
| `ca_ES-upc_ona-x_low.onnx.json` (https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ca/ca_ES/upc_ona/x_low/ca_ES-upc_ona-x_low.onnx.json?download=true) | x_low | 16000 |

So: **low/x_low = 16 kHz, medium/high = 22.05 kHz**. The low tier additionally
uses a smaller symbol set (amy-low `num_symbols 130`, piper 0.2.0 vs
amy-medium `num_symbols 256`, piper 1.0.0).

File sizes, measured locally 2026-10-12 (`ls -la ~/piper_models`, primary
measurement):

| File | Bytes | ~MiB |
| --- | --- | --- |
| `en_US-amy-medium.onnx` | 63,201,294 | ~60 |
| `en_GB-alan-medium.onnx` | 63,201,294 | ~60 (same architecture) |
| `zh_CN-huayan-medium.onnx` | 63,201,294 | ~60 |
| `en_US-ljspeech-high.onnx` | 114,199,011 | ~109 |
| `Kokoro-82M-v1.0.onnx` (different engine, for scale) | 163,234,740 | ~156 |

Low-tier `.onnx` size not measured locally (only medium/high voices are
downloaded) — record it when downloading (section 6). Expect smaller than
medium (16 kHz + smaller VITS), but that is an **estimate, not a claim**.

Recommendation: keep `en_US-amy-medium` (already the best amy; 60 MB,
22 kHz). Drop to `amy-low` only if disk/RAM is tight or batch TTS
throughput matters. "High" requires switching voices (lessac/ryan/ljspeech
high, ~109+ MB) — reserve for hero/narration audio. Piper is an onnxruntime
VITS inference (https://github.com/rhasspy/piper — repo archived
2025-10-06, development moved to
https://github.com/OHF-Voice/piper1-gpl), CPU-cheap relative to Whisper, so
TTS tier is a disk/traffic decision more than a latency one — verify with
per-character timing (section 6).

## 5. VAD segmentation to cut STT cost (primary)

Mechanism (https://github.com/ggml-org/whisper.cpp README "Voice Activity
Detection (VAD)"): with `--vad` + a VAD model, "first the audio samples are
passed through the VAD model which will detect speech segments. Using this
information, only the speech segments that are detected are extracted from
the original audio input and passed to whisper for processing. This reduces
the amount of audio data that needs to be processed by whisper and can
significantly speed up the transcription process."

- Model: `./models/download-vad-model.sh silero-v6.2.0` fetches
  `ggml-silero-v6.2.0.bin`, **864 KB** (size shown in the README's own
  download transcript). Alternatives listed via the script with no args.
  Upstream VAD repo: https://github.com/snakers4/silero-vad.
- VAD cost is negligible: "One audio chunk (30+ ms) takes less than 1 ms to
  be processed on a single CPU thread", "JIT model is around two megabytes",
  supports 8/16 kHz (https://github.com/snakers4/silero-vad README "Key
  Features"). ~3% overhead per chunk is noise next to seconds of encoder
  time.
- Savings math (arithmetic): transcribing only speech segments means cost
  ~= speech_fraction x full_cost. A podcast/interview with ~50% silence or
  pauses roughly halves encoder work; dense narration saves little. Measure
  the speech ratio per corpus (section 6) rather than assuming.
- Tuning knobs, same README section: `--vad-threshold`,
  `--vad-min-speech-duration-ms`, `--vad-min-silence-duration-ms`,
  `--vad-max-speech-duration-s`, `--vad-speech-pad-ms`,
  `--vad-samples-overlap`. Start from defaults, sweep threshold first;
  too-aggressive silence trimming clips word edges (mitigate with speech
  padding), too-lax keeps the cost. Note VAD model lives in the
  https://huggingface.co/ggml-org/whisper-vad collection.
- whisper.cpp also ships experimental `--tdrz` tinydiarize speaker
  segmentation (small.en-tdrz model, 465 MiB — same README/models table),
  but that is diarization, not a cost saver; do not conflate with VAD.

## 6. What to measure (target host, CPU-only, `-t 2`)

1. **Re-download the model first.** `whisper_models/` currently holds only
   the 29-byte placeholder (section 0): e.g.
   `sh ./models/download-ggml-model.sh base.en` (script documented in
   https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md),
   plus each ladder rung `tiny.en base.en small.en` and quantized variants
   from https://huggingface.co/ggerganov/whisper.cpp.
2. **Encoder microbenchmark per rung:**
   `whisper-bench -m <model> -t 2 [-ng]` and record Load/Enc/Dec/total
   (same fields as https://github.com/ggml-org/whisper.cpp/issues/89).
   Compute encoder RTF = 30000 / Enc_ms. Confirm the binary reports
   AVX2/FMA (cf. the 5700X non-AVX2 outlier, section 3).
3. **End-to-end wall time:** `whisper-cli -m <model> -f <30 s, 5 min, 30 min
   wavs> -t 2` with the repo's timing printout; RTF = audio_sec /
   wall_sec. Repeat for turbo-q5_0 to replace the section-2 extrapolation
   with a number. Check `whisper-server --help` for the thread/parallel
   flags actually honored by the compose `whisper-server` command.
4. **VAD A/B:** download `silero-v6.2.0`
   (`./models/download-vad-model.sh silero-v6.2.0`), then with/without
   `--vad -vm ggml-silero-v6.2.0.bin` on a long file with known
   speech/silence ratio; report wall time, WER spot-check (clipping), and
   sweep `--vad-threshold` + `--vad-speech-pad-ms`.
5. **RAM:** `/usr/bin/time -v whisper-cli ...` (Maximum resident) or
   `docker stats` for the compose services; verify the ~273/388/852 MB
   README figures and the ~770 MB turbo-q5_0 figure on this host.
6. **Piper:** `ls -la` the downloaded low-tier `.onnx` (missing local
   datum, section 4); time seconds-of-audio per second-of-wall per tier
   and per voice; blind-listen amy-low vs amy-medium vs lessac-high before
   paying 2x disk for high.
7. **Decision rule:** default to the largest ladder rung that holds
   encoder RTF >= ~3x end-to-end on 2 threads with headroom for concurrent
   services; gate turbo behind a size/duration threshold (e.g. short clips
   only) or a manual quality flag.
