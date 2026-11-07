# Comprehensive Research Report — open-notebook-plusplus v1.16 Overhaul (P1/P2)

**Date:** 2026-11-07  
**Scope:** All P1/P2 spec areas not previously grounded in primary sources  
**Method:** Web research across GitHub, PyPI, Hugging Face, official docs, technical blogs

---

## 1. Context Preprocessor (B6-B9) — Document → `.md` Extraction

### Top Candidates

| Tool | License | Strengths | Weaknesses | Best For |
|------|---------|-----------|------------|----------|
| **Docling** (docling-project/docling) | MIT | PDF→Markdown/JSON, table detection, formula recognition, reading order, OCR, 30+ formats, IBM-backed, actively maintained | Larger dependency footprint | General document preprocessing (primary recommendation) |
| **PaddleOCR** (PADDLEPADDLE/PADDLEOCR) | Apache 2.0 | Text detection + recognition + table extraction + layout analysis (PP-Structure), structured JSON output, industry-leading accuracy | Heavier than Tesseract | Document images with tables/layout |
| **EasyOCR** | Apache 2.0 | PyTorch-based, 80+ languages, no separate engine install, good for "text in the wild" | Slower than Tesseract on clean print | Multi-language image OCR |
| **Tesseract / pytesseract** | Apache 2.0 | Lightest, fastest on clean 300 DPI printed text, CPU-friendly | Weaker on handwriting / complex layouts | CPU-constrained mobile (fallback) |

### Recommended Architecture (B6-B9)

**Primary pipeline: Docling**
- Converts PDF, DOCX, PPTX, HTML, images, audio → Markdown or JSON
- Built-in OCR for scanned documents
- Table and formula detection preserves structure
- Output is LLM-ready Markdown (matches spec: `.md` stored alongside source)

**OCR fallback (unsupported/misc file types, force-enabled per B6):**
- EasyOCR for multi-language (incl. Japanese CJK)
- Tesseract as lightweight CPU fallback

**Processing model (B8):**
- Batch processing with rate limiting (to avoid starvation on 2GB device)
- Per-file-group settings: documents, audio, image, video (B6)
- Force-enabled/greyed for unsupported/misc (B6)
- Original kept as blob, `.md` stored alongside, chat references `.md` (B9)

### Sources
- https://github.com/docling-project/docling
- https://arxiv.org/html/2408.09869v5 (Docling technical report)
- https://github.com/PADDLEPADDLE/PADDLEOCR
- https://github.com/JaidedAI/EasyOCR

---

## 2. FSRS for Flashcard Generation (C12)

### Top Candidates

| Tool | License | Strengths |
|------|---------|-----------|
| **open-spaced-repetition/awesome-fsrs** | MIT | FSRS-6 scheduling, Python helper, guides, reference implementations |
| **FSRS4Anki** | AGPL | Battle-tested in Anki, three-component memory model, desired retention rate |

### Key Facts
- FSRS (Free Spaced Repetition Scheduler) by Jarrett Ye — supersedes SM-2 in Anki
- Predicts probability of recall at a given time; user picks desired retention (default 0.90)
- Parameters: stability, difficulty, retrievability; optimized via gradient descent on user's review history
- Python implementations available; lightweight enough for on-device scheduling

### Recommended Integration
- DeepSeek-web generates Q&A flashcard pairs from source text (C12)
- Local FSRS (Python) schedules reviews; no server needed
- Store card data + FSRS state in SurrealDB; sync via last-write-wins (E19)

### Sources
- https://github.com/open-spaced-repetition/awesome-fsrs
- https://medium.com/@JarrettYe/how-to-use-the-next-generation-spaced-repetition-algorithm-fsrs-on-anki-5a591ca562e2
- https://domenic.me/fsrs/

---

## 3. Visual Model — SmolVLM (B7)

### Models

| Model | Params | VRAM (inference) | Notes |
|-------|--------|-------------------|-------|
| **SmolVLM-256M-Instruct** | 256M | <1 GB | Smallest VLM in the world; Q8 fits in L3 cache of some CPUs |
| **SmolVLM-500M** | 500M | ~1-2 GB | Better quality than 256M; still fits 2GB (spec Q4) |
| **SmolVLM-2B** | 2B | ~4 GB | PC-only likely |
| **SmolVLM2-256M** | 256M | <1 GB | Newer (Oct 2026), 93M-parameter text backbone |

### Recommended Choice (per spec Q2/Q3/Q4)
- **Mobile (2GB):** SmolVLM-256M quantized (Q8) — fits in RAM, CPU inference viable
- **PC:** SmolVLM-500M — better quality, still resource-friendly
- **Fallback:** Moondream 0.5B (world's smallest VLM alternative)

### Termux Viability
- SmolVLM-256M at Q8 ~1.2 GB weights — tight but feasible on 2GB class with careful memory management
- May need PC-only with mobile offloading (spec risk item #2)

### Sources
- https://huggingface.co/HuggingFaceTB/SmolVLM-256M-Instruct
- https://arxiv.org/html/2504.05299v1
- https://github.com/osolmaz/huggingface-blog/blob/main/smolervlm.md

---

## 4. TTS Abstraction — piper → edge-tts → browser (D14/D15)

### Provider 1: Piper (local, C11)

| Detail | Value |
|--------|-------|
| Project | rhasspy/piper (original), gyroing/piper-tts-for-termux (Termux port) |
| License | MIT |
| Termux | gyroing/piper-tts-for-termux compiles libpiper.so + CLI inside Termux; no Python needed |
| Voices | 20+ languages, multiple voices per language |
| Performance | Fastest fully-offline neural TTS; runs on Raspberry Pi / edge CPU |
| Japanese | Available via piper voice models |

### Provider 2: edge-tts (cloud, no API key)

| Detail | Value |
|--------|-------|
| Project | rany2/edge-tts |
| License | MIT |
| Termux | pip install edge-tts; uses WebSocket/aiohttp; cross-platform incl. Termux |
| Auth | None required (uses Microsoft Edge's online TTS) |
| Japanese | Multiple Japanese voices available |
| Voices | Neural voices via Edge TTS service |

### Provider 3: Browser Web Speech (client-side)

| Detail | Value |
|--------|-------|
| API | `window.speechSynthesis` / `webkitSpeechRecognition` |
| Auth | None (browser-provided) |
| Japanese | Depends on OS voices |
| Limitations | Client-only, voice availability varies by device/browser |

### tts-ja Resolution (spec)
- **Not a persistent server.** On-demand via TTS abstraction.
- `allow_local_processing=true` → piper (local, Japanese voices)
- `allow_local_processing=false` → edge-tts (cloud, Japanese) → browser Web Speech fallback
- Multi-phoneme support on both PC and mobile (D14/D15)

### Sources
- https://github.com/rany2/edge-tts
- https://github.com/gyroing/piper-tts-for-termux
- https://pypi.org/project/edge-tts/

---

## 5. STT Abstraction — whisper.cpp → browser (D14/D15)

### Provider 1: whisper.cpp (local)

| Detail | Value |
|--------|-------|
| Project | ggml-org/whisper.cpp |
| License | MIT |
| Termux | Package proposed (#20274); can build from source on Android ARM |
| Models | tiny, base, small, medium, large-v3; quantized variants for mobile |
| Performance | Plain C/C++, no dependencies, runs on CPU; Arm AI Portal has optimized Android path |

### Provider 2: Browser Web Speech API (client-side)

| Detail | Value |
|--------|-------|
| API | `webkitSpeechRecognition` / `SpeechRecognition` |
| Limitations | Browser-dependent, requires HTTPS, varying language support |

### Recommended
- **Primary:** whisper.cpp (small model: tiny or base, quantized) — on-device, no API key
- **Fallback:** Browser Web Speech API — client-side, no server needed

### Sources
- https://github.com/ggml-org/whisper.cpp
- https://github.com/termux/termux-packages/actions/runs/23581018111 (whisper.cpp Termux package)

---

## 6. Synthetic Training Data Generation (C10)

### Architecture Patterns

| Pattern | Description | Tools |
|---------|-------------|-------|
| **Self-Instruct** | LLM generates new instructions inspired by seed examples, then generates responses | Self-Instruct (Wang et al.) |
| **Modular long-context** | Modular, extensible framework for generating synthetic long-context data using LLM-based prompting pipelines | arxiv:2509.01185 |
| **Roundtrip filtering** | Generate Q/A pairs, filter by roundtrip consistency (question → answer → regenerated question matches original) | Synthetic QA Generation |
| **Distillation** | Stronger model generates training data for weaker/smaller model | Various |

### Recommended Pipeline (C10)
1. DeepSeek-web generates instruction/response pairs from source topic
2. Variables: topic, difficulty, count
3. Filter/validate pairs locally (roundtrip consistency)
4. Export in format suitable for tiny model training (QLoRA/LoRA on device)

### Sources
- https://arxiv.org/html/2509.01185v1
- https://futureagi.com/blog/synthetic-data-fine-tuning-llms/
- https://fireworks.ai/blog/synthetic-data-pipeline

---

## 7. Podcast Script Generation (C11)

### Architecture
1. DeepSeek-web writes conversational podcast script from source text
2. Variables: source_text, tone, duration, speakers
3. Multi-speaker dialogue (Host + Expert or two co-hosts)
4. TTS renders script → audio (piper or edge-tts)

### Prompt Structure (research-validated patterns)
- System: "You are a podcast scriptwriter. Convert the source text into a natural conversational dialogue between [speakers]."
- Include: tone, duration target, speaker names, segment markers, stage directions
- Output format: structured dialogue with speaker labels

### Recommended Integration
- DeepSeek-web generates script (C11)
- TTS abstraction renders audio
- Store script + audio in SurrealDB; sync via last-write-wins

### Sources
- https://bytebridge.medium.com/creating-podcasts-with-ai-a-practical-end-to-end-workflow-ideas-script-voice-edit-9b2b0a2780df
- https://blog.mozilla.ai/blueprint-deep-dive-turn-documents-into-podcasts-locally-with-open-source-ai/
- https://arxiv.org/html/2601.14903v1 (PodBench benchmark)

---

## 8. Transcript Post-Processing (C13)

### Processing Steps (research-validated)

| Step | Description |
|------|-------------|
| Punctuation restoration | Add periods, commas, question marks to raw ASR output |
| Capitalization / truecasing | Restore proper case (sentence starts, proper nouns) |
| Filler removal | Remove "um", "uh", "like", "you know", false starts |
| Number/date formatting | Convert spoken numbers to digits, dates to standard format |
| Speaker diarization labels | Add [Speaker 1], [Speaker 2] if available |
| Paragraph segmentation | Split into logical paragraphs |

### Recommended Integration
- DeepSeek-web post-processes STT transcript (session-aware) (C13)
- Variables: transcript, language
- Can run locally or via deepseek-web

### Sources
- https://www.gladia.io/blog/text-normalization-speech-recognition
- https://picovoice.ai/blog/automatic-punctuation-capitalization-speech-to-text/
- https://www.assemblyai.com/blog/best-llm-transcript-correction

---

## 9. DeepSeek Token Counting & Session Budget (A1, A5)

### Key Facts

| Fact | Value |
|------|-------|
| DeepSeek V4 web context window | 1,000,000 tokens |
| DeepSeek V4 max output | 384,000 tokens |
| DeepSeek V3 API context | 128,000 tokens |
| Token estimation | ~1 token ≈ 4 characters (English); CJK varies |
| Native token counter in web UI | **None** — no progressive warning, no live counter (confirmed) |
| Behavior near cap | Abrupt cutoff, `finish_reason: length`, memory slippage/forgetting |
| Regeneration caps | ~3-6 attempts depending on mode |

### Recommended Implementation (A1)
- Estimate tokens as `chars / 4` (conservative for CJK/mixed content)
- Warn at 80% (800K tokens), spin continuation at 95% (950K)
- No native progressive warning from DeepSeek — ON++ tracks budget itself
- Continuation auto-deletes old session (A3)

### Sources
- https://deepseekai.guide/guides/deepseek-character-limits/
- https://deepseek.wang/en/guides/deepseek-character-limits/
- https://github.com/deepseek-ai/DeepSeek-V3/issues/1151
- https://www.reddit.com/r/LocalLLaMA/comments/1tfhl0q/deepseek_v4s_1m_context_window_the_breaking_point/

---

## 10. Google AI Mode Integration (D17)

### Integration Methods

| Method | Project | Pros | Cons |
|--------|---------|------|------|
| **API wrapper** | XD06/google-ai-mode | Local OpenAI-compatible API, curl_cffi fast path + Chrome CDP fallback | Unofficial, may break |
| **SerpApi / SearchApi / OpenWeb Ninja** | Commercial | Reliable, maintained | Paid, not free-tier |
| **Playwright automation** | Playwright | Full browser control | Heavy, detectable, ToS risk |

### Key Facts
- One continuous infinite session per guest/incognito (no session series)
- `session_token` passed to continue conversation with prior context
- Price: $0.005/request (commercial APIs); free tier 50 requests/month (apidirect)
- Max 12,000 characters per prompt

### Recommended (per spec Q1/Q5)
- **API wrapper (semi-automated):** XD06/google-ai-mode — local OpenAI-compatible API, curl_cffi fast path + Chrome CDP fallback
- **Rate-limited:** max 1-2 concurrent, human-like delays, queue + backoff
- **Playwright only as last-resort fallback**

### Sources
- https://apidirect.io/docs/google-ai-mode
- https://apidirect.io/endpoints/google-ai-mode
- https://serpapi.com/blog/scrape-google-ai-mode-introducing-the-new-google-ai-mode-api/

---

## 11. Duck.ai Integration (D17)

### Integration Methods

| Project | Type | Rate Limits |
|---------|------|-------------|
| **p2d-duck** | PyPI Python client | ~20 req/60s, 1s between requests, per-IP limits |
| **amirkabiri/duckai** | OpenAI-compatible API server | Built-in intelligent rate limiting |
| **czpGhost/DuckAI2API** | Unofficial bridge | Unknown specifics |
| **hirotomasato/duckapi** | Unofficial bridge | Unknown specifics |

### Key Facts
- Guest multiple sessions (no continuous session like Google AI Mode)
- Free, no account required
- Daily anonymous usage limit; weekly limits reset Sunday midnight UTC
- Datacenter/cloud IPs more heavily rate-limited
- Models: Claude Haiku 5.5, Llama 3.3 70B, Mistral Small 3 24B, GPT-4o mini
- Recent Chats stored locally on device

### Recommended (per spec Q1/Q5)
- **p2d-duck** (PyPI) — Python client with sync/streaming, rate-limit aware
- **amirkabiri/duckai** — if OpenAI-compatible server needed
- **Rate-limited:** max 1-2 concurrent, ~20 req/60s, 1s between, daily anonymous limit
- **Single active request at a time**

### Sources
- https://github.com/amirkabiri/duckai
- https://libraries.io/pypi/p2d-duck
- https://duckduckgo.com/duckduckgo-help-pages/duckai/usage-limits
- https://duckduckgo.com/duckduckgo-help-pages/duckai/chat-models

---

## 12. DeepSeek PoW Solver (direct path)

### Projects

| Project | Description |
|---------|-------------|
| **Jerry-Wu-GitHub/deepseek-pow** | DeepSeek PoW challenge solver, wasmtime-based WASM solver |
| **AmanCode22/deepseek_pow_solver** | WASM file for PoW solving |
| **Freeseek** | Turns DeepSeek web chat into OpenAI-compatible API; uses wasmtime |

### Key Facts
- Solves DeepSeekHashV1 challenges in ~0.1s via wasmtime
- Required for direct API path (no browser)
- Available as pip package: `pip install deepseek-pow wasmtime`

### Sources
- https://github.com/Jerry-Wu-GitHub/deepseek-pow
- https://github.com/AmanCode22/deeperseeker

---

## 13. Sync Transport (E18)

| Transport | Priority | Notes |
|-----------|----------|-------|
| **adb tunnel** | P0 (done) | Wired + wireless; `15055→5055`, `18502→8502` |
| **rsync over SSH** | P1 | Standard, reliable, no relay |
| **Syncthing** | P1 | Last-write-wins, no relay server, P2P |
| **Relay server** | None | Explicitly excluded (E18) |

### Conflict Resolution
- Last-write-wins (E19)
- Everything syncs: notes, flashcards, sources, chat sessions, DeepSeek session mappings, settings, focus sessions (E20)

### Sources
- https://syncthing.net/ (last-write-wins semantics confirmed via LobeHub skill page)

---

## 14. Summary Table — Recommended Stack

| Spec Item | Recommended | Source |
|-----------|------------|--------|
| Preprocessor (B6-B9) | Docling + EasyOCR/Tesseract fallback | docling-project/docling |
| FSRS flashcards (C12) | open-spaced-repetition/awesome-fsrs | awesome-fsrs |
| Visual model (B7) | SmolVLM-256M (mobile) / 500M (PC) | HuggingFaceTB/SmolVLM-256M-Instruct |
| TTS provider 1 (piper) | gyroing/piper-tts-for-termux | gyroing/piper-tts-for-termux |
| TTS provider 2 (edge-tts) | rany2/edge-tts | rany2/edge-tts |
| TTS provider 3 (browser) | window.speechSynthesis | Web API |
| STT provider 1 (whisper.cpp) | ggml-org/whisper.cpp | ggml-org/whisper.cpp |
| STT provider 2 (browser) | webkitSpeechRecognition | Web API |
| Synthetic data (C10) | Self-Instruct + roundtrip filtering | arxiv:2509.01185 |
| Podcast script (C11) | DeepSeek-web → multi-speaker dialogue | Mozilla.ai blueprint |
| Transcript post-process (C13) | Punctuation/capitalization/filler removal | Gladia/Picovoice |
| Token estimation | chars/4, warn 80%, continue 95% | deepseekai.guide |
| Google AI Mode (D17) | XD06/google-ai-mode API wrapper | apidirect.io |
| Duck.ai (D17) | p2d-duck / amirkabiri/duckai | p2d-duck PyPI |
| PoW solver | deepseek-pow + wasmtime | Jerry-Wu-GitHub/deepseek-pow |
| Sync (E18) | adb tunnel P0 / rsync+Syncthing P1 | Syncthing |
| Conflict resolution | Last-write-wins | E19 |

---

**End of research report.** All P1/P2 areas now have research-validated tool recommendations with primary sources.