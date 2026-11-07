# LOCKED SPEC — open-notebook-plusplus Complete Overhaul (v1.16)

**Status:** LOCKED — approved by learner, ready for implementation  
**Base:** commit `6b8a14e4` on `plusplus/main`  
**Phone:** CPH2333 Android 13, SSH `u0_a328@10.7.115.125:8022`, API `:5055`, UI `:8502`  
**PC:** dev-box, builds frontend standalone, pushes via route B  
**Constraint:** Never push to `origin` (lfnovo); all pushes to `plusplus/main`. Full battery per change: pytest + ruff check/format + mypy (full tree) + frontend tsc/lint/vitest/build; shellcheck `-S warning` clean on device scripts.

---

## 1. Core Principles (locked)

| Principle | Decision |
|-----------|----------|
| Cloud-first, local-second | DeepSeek-web is primary inference; local llama/embed/TTS/STT opt-in via `allow_local_processing` (default disabled) |
| Session series (A1) | One ON++ session → series of DeepSeek sessions (~1M token cap each). Lazy DeepSeek creation on first message. Compaction prunes only DeepSeek side; ON++ preserves compacted context. |
| One-way delete (A3) | ON++ deletion cascades to DeepSeek. DeepSeek deletion does NOT cascade back. Continuation auto-deletes old DeepSeek session. |
| Classification (A2/A5) | One classification per session row (unique enum). Scheduling + rate limiting for concurrent sessions (ToS avoidance). |
| Preprocessor (B6-B9) | Per-file-group settings; force-enabled for unsupported/misc. Extract to `.md` (OCR + captioning). `.md` alongside source, original as blob, chat references `.md`. Batch + rate-limited. |
| Local degradation (H26/H27) | Cloud needs internet; graceful local fallback offline. No daily message limit. |
| tts-ja | On-demand only (not persistent), via TTS abstraction piper → edge-tts → browser. |

---

## 2. Session Architecture (A1 — locked)

**Option 1: Session series.** Every ON++ functionality that talks to a model gets its own session series:
- `chat` — one notebook chat session = multiple DeepSeek session series (continuation chain)
- `insight` — dedicated shared insight generation session per notebook
- `embedding` — dedicated embedding session per notebook
- `tiny_training` — one ON++ session series per model
- `podcast` — one ON++ session series per podcast (multiple seasons/episodes)
- `flashcard` — one ON++ session series per notebook
- `stt` — one ON++ session series per STT session
- `tts` — one ON++ session series per TTS series
- `search` — knowledge base / research stack session (multiple ON++ session series)
- `source_chat` — per source chat session

**DeepSeek session lifecycle:**
- Lazy creation: new DeepSeek session created only when user types first message (with or without attachments)
- Subsequent messages in same ON++ session reuse the same DeepSeek session
- Token cap ~1M per DeepSeek session (input + output + reasoning/hidden thinking tokens)
- No native progressive warning from DeepSeek web; ON++ tracks token budget itself
- Warn at 80% token budget, spin new continuation session at 95%
- Continuation session auto-deletes previous DeepSeek session (A3)
- Original conversation pruned only from DeepSeek side; model remembering via context provisioning from ON++ compacted conversations in the new continuation session

**Concurrency & rate limiting (A5):**
- Max 1-2 concurrent DeepSeek requests
- Queue remaining requests with backoff
- Human-like delays to avoid automation detection / ToS

---

## 3. Database Schema (SurrealDB)

### New table: `deepseek_sessions`

```sql
DEFINE TABLE deepseek_sessions SCHEMAFULL;
DEFINE FIELD notebook_id ON deepseek_sessions TYPE string;
DEFINE FIELD deepseek_session_id ON deepseek_sessions TYPE string;
DEFINE FIELD classification ON deepseek_sessions TYPE string; -- chat|insight|embedding|tiny_training|podcast|flashcard|stt|tts|search|source_chat
DEFINE FIELD token_count ON deepseek_sessions TYPE number DEFAULT 0;
DEFINE FIELD parent_deepseek_session_id ON deepseek_sessions TYPE option<string>; -- continuation chain
DEFINE FIELD status ON deepseek_sessions TYPE string DEFAULT 'active'; -- active|completed|archived
DEFINE FIELD created_at ON deepseek_sessions TYPE datetime;
DEFINE FIELD last_used_at ON deepseek_sessions TYPE datetime;
DEFINE INDEX deepseek_sessions_notebook ON deepseek_sessions COLLECTIONS notebook_id;
DEFINE INDEX deepseek_sessions_classification ON deepseek_sessions COLLECTIONS classification;
DEFINE INDEX deepseek_sessions_parent ON deepseek_sessions COLLECTIONS parent_deepseek_session_id;
```

### Modify `chat_sessions` (existing)

Add column `deepseek_session_series_id` → references `deepseek_sessions` root session. One classification per `chat_sessions` row.

### Classification enum (locked, unique per row)

```
chat | insight | embedding | tiny_training | podcast | flashcard | stt | tts | search | source_chat
```

**Rule:** A session handles only one classification. A notebook can have multiple chat session series; one dedicated session each for embedding / insight / transformation / etc.

---

## 4. New Services

### 4.1 `open_notebook/services/deepseek_session_service.py` — DeepSeek Session Lifecycle

Responsibilities:
- Lazily create DeepSeek web session on first message (A1)
- Track token usage against 1M cap per DeepSeek session
- Warn at 80% token budget, spin new continuation session at 95%
- Auto-delete old DeepSeek session on continuation (A3)
- Return `(deepseek_session_id, is_continuation, token_budget_remaining)` per request
- Rate-limit concurrent sessions (max 1-2, queue + backoff, human-like delays)

### 4.2 `open_notebook/services/preprocessor_service.py` — Context Preprocessor

Responsibilities:
- Per-file-group settings: documents, audio, image, video (B6)
- Force-enabled/greyed for unsupported/misc file types (app bundles, 3D models, project files, niche extensions) (B6)
- Extract to `.md`: OCR text for documents/images, captioning for images via SmolVLM, audio transcription via STT (B7/B9)
- Batch processing with rate limiting (B8)
- Store `.md` alongside source (linked artifact), original kept as blob (B9)
- Chat references `.md`, not reprocessing (B9)

### 4.3 `open_notebook/services/tts_service.py` — TTS Abstraction

Providers in priority order:
1. **piper** — local (already in termux stack), renders audio locally, has Japanese voices (C11)
2. **edge-tts** — cloud fallback via Microsoft Edge online TTS, no API key (researched: `rany2/edge-tts`, WebSocket/aiohttp, cross-platform incl. Termux)
3. **browser-web-speech** — client-side via `window.speechSynthesis` / `webkitSpeechRecognition` (D14/D15)

**tts-ja resolution:** Not a persistent server. On-demand activation — TTS service starts only when `POST /api/tts/synthesize` is called with Japanese text, synthesizes, then stops. No persistent `:8092` server. `allow_local_processing=true` enables local piper TTS; when off, falls back to edge-tts cloud. Multi-phoneme support on both PC and mobile (D14/D15).

### 4.4 `open_notebook/services/stt_service.py` — STT Abstraction

- **whisper.cpp** — local (already in stack)
- **Browser Web Speech API** — client-side STT (D14/D15)
- Direct translate fallback if necessary

### 4.5 `open_notebook/services/browser_ai_service.py` — Browser AI Mode Integration (D17)

- **Google AI Mode:** one continuous infinite session per guest/incognito web search, wired to one ON++ session, no session series/continuation. Integration via API wrapper (`XD06/google-ai-mode` — local OpenAI-compatible API, curl_cffi fast path + Chrome CDP fallback). Semi-automated, rate-limited.
- **Duck.ai:** guest multiple sessions. Integration via unofficial bridges (`p2d-duck`, `czpGhost/DuckAI2API`, `hirotomasato/duckapi`). Rate limits: ~20 req/60s, 1s between requests, daily anonymous limit. Single active request at a time. Ban risk on IP — rate-limit strictly.

**Q1 answer:** API wrapper (semi-automated), not full Playwright. Playwright only as last-resort fallback.

---

## 5. Backend API Changes

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/models` | GET | Already returns `deepseek-web` virtual model (done in `6b8a14e4`) |
| `POST /api/chat/execute` | POST | Routes `model_override == "deepseek-web"` through direct PoW solver (done in `c2ad3802`) |
| `POST /api/deepseek-sessions` | POST | List/create DeepSeek sessions with classification |
| `DELETE /api/deepseek-sessions/{id}` | DELETE | Cascades to DeepSeek web session deletion (one-way, A3) |
| `POST /api/preprocess` | POST | Batch preprocess files to `.md` |
| `POST /api/tts/synthesize` | POST | Abstracted TTS (on-demand, piper → edge-tts → browser) |
| `POST /api/stt/transcribe` | POST | Abstracted STT (on-demand, whisper.cpp → browser) |
| `POST /api/podcast/script` | POST | DeepSeek-web writes podcast script → TTS render (C11) |
| `POST /api/flashcards/generate` | POST | DeepSeek-web generates flashcards → local FSRS (C12) |
| `POST /api/training/synthetic-data` | POST | DeepSeek-web generates synthetic training pairs (C10) |
| `POST /api/transcript/postprocess` | POST | DeepSeek-web post-processes transcripts (session-aware) (C13) |

Rate-limiting middleware per DeepSeek session (A5).

---

## 6. Frontend/UI Changes

| Area | Change |
|------|--------|
| **ModelSelector** | Add `deepseek-web` option everywhere relevant (F23): chat, transformations, insights, podcasts, flashcard gen, STT/TTS post-processing. Gated on `pow_solver_available` + credentials (from `/api/models`). Falls back to PC URL when configured. |
| **DeepSeek Session View** | Per notebook session: show DeepSeek session series (token count, continuation chain, status). One-way delete: delete ON++ session → confirms DeepSeek session deletion too. |
| **Settings — Context Preprocessor** | Per-file-group toggles: documents, audio, image, video. Force-enabled/greyed for unsupported/misc. Visual model selection (SmolVLM 256M/500M). |
| **Settings — TTS/STT** | Provider selection: piper (local), edge-tts (cloud), browser-web-speech (client). Voice selection, multi-phoneme support. tts-ja removed as persistent server. |
| **UI overhaul** | Last resort (F21). Simplify/improve performance now. Navigation/responsive postponed (F22). |

---

## 7. Deployment

### PC (dev box)
- Build frontend standalone: `INTERNAL_API_URL=http://127.0.0.1:5055 next build --webpack`
- Run local services: llama `:8080`, embed `:8081`, piper TTS, browser AI bridges (Google AI Mode wrapper, Duck.ai bridge, edge-tts Python service)
- adb tunnel: `15055→5055`, `18502→8502`

### Mobile (phone, Termux + proot)
- `onb-stack up` starts: surreal, uvicorn, next-server, llama, embed
- `allow_local_processing` flag read at launcher startup (not runtime toggle)
- TTS on-demand only (no persistent tts-ja server)
- Frontend: route B (atomic `frontend.new` → `frontend` swap)

### Sync transport (E18)
- **P0:** adb tunnel (wired + wireless)
- **P1:** rsync over SSH / Syncthing
- **No relay server**
- **Conflict resolution:** last-write-wins (E19)
- **Everything syncs** (E20): notes, flashcards, sources, chat sessions, DeepSeek session mappings, settings, focus sessions

---

## 8. Sync Architecture

- Bidirectional sync of DeepSeek session ↔ ON++ session mappings
- One-way delete cascade: ON++ delete → DeepSeek delete (A3)
- Last-write-wins for content conflicts (E19)
- adb tunnel as primary transport (E18 P0)

---

## 9. Prompt Library (G24/G25)

Versioned prompts per feature (research-validated, no hallucination, optional custom prompts appendable):

| Feature | Prompt Purpose | Variables |
|---------|---------------|-----------|
| Flashcard generation | Q&A from source text | source_text, language, card_type |
| Insight generation | Extract key insights | source_text, depth_level |
| Podcast script | Write podcast script | source_text, tone, duration, speakers |
| Transformation | Rewrite/summarize/analyze | source_text, transform_type, target_audience |
| Source summarization | Summarize document | source_text, max_length |
| Context preprocessing | Extract `.md` from file | file_content, file_type, ocr_language |
| Synthetic training data | Generate instruction/response pairs | topic, difficulty, count |
| Transcript post-processing | Clean up STT transcript | transcript, language |

---

## 10. Rate Limiting & Scheduling (A5)

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max concurrent DeepSeek requests | 1-2 | Avoid automation detection / ToS |
| DeepSeek session token cap | 1M tokens | DeepSeek-V4 web limit |
| Warn at token budget | 80% | DeepSeek web has no native progressive warning |
| Spin new continuation session | 95% | Before abrupt cutoff / memory slippage |
| Duck.ai rate limit | ~20 req/60s, 1s between | Researched limits |
| Google AI Mode | per session_token | One continuous session |

---

## 11. Visual Model Choice (B7)

**Primary:** SmolVLM 500M (Hugging Face, SOTA memory footprint, 256M/500M/2B sizes, fully open-source)
- **Mobile (2GB):** SmolVLM 256M or 500M quantized
- **PC:** SmolVLM 500M or 2B
- **Fallback:** Moondream 0.5B (world's smallest VLM)

Runs on both PC and mobile with resource management (no starvation). Q4: SmolVLM 500M is the better alternative (better quality than 256M, still fits 2GB).

---

## 12. Implementation Order (P0 → P2)

### P0 (do now)
1. Frontend rebuild + redeploy to phone (route B) — `deepseek-web` in ModelSelector
2. `deepseek_session_service` — lazy creation, token tracking, continuation, auto-delete
3. `deepseek_sessions` DB table + classification enum
4. One-way delete cascade (ON++ → DeepSeek)

### P1
5. Preprocessor service (.md extraction, OCR, captioning, batch + rate limit)
6. TTS/STT abstraction (piper + edge-tts + browser Web Speech)
7. Flashcard generation (DeepSeek-web → local FSRS)
8. Podcast script generation (DeepSeek-web → TTS render)
9. Prompt library (researched prompts)

### P2
10. Google AI Mode + Duck.ai browser integration
11. Sync (adb tunnel P0 done, rsync/Syncthing P1)
12. Tiny model training with synthetic data
13. Transcript post-processing
14. UI overhaul (last resort)

---

## 13. Risks

| Risk | Mitigation |
|------|------------|
| Google AI Mode / Duck.ai unofficial — ban risk on IP | Strict rate limiting, human-like delays, max 1-2 concurrent |
| Visual model on Termux (CPU, 2GB) | SmolVLM 256M/500M; PC-only with mobile offloading if needed |
| edge-tts on Termux | Test on mobile; fallback to browser Web Speech |
| DeepSeek token counting — no native counter | Estimate chars/4 |
| tts-ja persistent server DOWN | On-demand TTS abstraction, no persistent server |

---

## 14. Current State (as of spec lock)

- Repo: `/home/dev-sudo-senpai/open-notebook`, branch `main`, at `6b8a14e4` (pushed to `plusplus/main`)
- Phone: live via SSH `u0_a328@10.7.115.125:8022`, API `:5055` healthy, llama `:8080` up, embed `:8081` up, tts-ja DOWN
- Backend: DeepSeek truthful PoW + whole-fragment SSE (4376590), chat UI wired to direct solver (c2ad3802), `deepseek-web` virtual model in `/api/models` (6b8a14e4)
- Frontend: NOT yet rebuilt/redeployed — needs `next build --webpack` + route B atomic swap
- Tests: pytest 2433 pass / 10 env-fail, vitest 252/252, tsc/ruff/mypy/eslint clean
- All prior decisions (A1-H28) locked; open questions Q1-Q5 answered; tts-ja resolved as on-demand via TTS abstraction

---

**End of spec.** Awaiting green flag before implementation begins.