# open-notebook-plusplus — Test Results (compact, live phone v1.15.1)

**Phone:** CPH2333, API `:5055` (PC fwd `15055`), UI `:8502` (fwd `18502`), Bearer `eeUf2oJkuQBT1ma9ngtNUfny1k6N`
**State:** allow_local_processing=False (toggled True via API but :8080/:8081 not running — flag read at launcher startup, needs stack restart)
**Models:** bge-small emb, bge-m3 emb, qwen2.5-0.5b lang (files present, servers down)

## ✅ WORKING (verified live)

| Category | Endpoint | Result |
|----------|----------|--------|
| Core | GET /health, /config, /models, /settings | 200, healthy, v1.15.1, 2emb+1lang |
| DeepSeek direct PoW | POST /api/chat/deepseek-web | pow_solved:true, pc_fallback:false |
| DeepSeek session | GET /api/chat/deepseek-web/session | pow_solver_available:true |
| **DeepSeek via notebook chat (NEWLY WIRED)** | POST /api/chat/execute + model_override=deepseek-web | **"NOTEBOOK WIRED PATH WORKS"** ✅ |
| Settings put | PUT /api/settings allow_local_processing=true | accepted |
| Sources create | POST /api/sources (form-data type=text) | creates, but POST hangs awaiting processing |
| Sources list | GET /api/sources | 16 sources, status/embedded visible |
| Search text | POST /api/search | relevance scores returned |
| Flashcards | POST/GET /api/flashcards | CRUD works, needs `model` field |
| Flashcard approve | POST /api/flashcards/{id}/approve | status→approved, guid generated |
| FSRS review grade | POST /api/review/grade/{id} rating=good | card scheduled, 0 due |
| Review due/retention | GET /api/review/due, /retention | [], 0.85 |
| Focus | GET /api/focus/sessions/open | [] works |
| Metrics | GET /api/metrics/summary | all zeros (no activity) |
| Capabilities | GET /api/capabilities | docling/crawl4ai false (opt-in) |
| Governor | GET /api/governor/state | asleep:false |
| Transformations | GET /api/transformations | list returned |
| Episode/speaker profiles | GET /api/episode-profiles, /speaker-profiles | lists returned |
| Notebooks | GET/POST /api/notebooks | 8 notebooks, create works |
| Chat sessions | POST /api/chat/sessions | create works, model_override persisted |
| Source chat session | POST /api/sources/{id}/chat/sessions | session created |

## ⚠️ PARTIAL / NEEDS ATTENTION

| Issue | Detail |
|-------|--------|
| POST /api/sources hangs | Creates source (visible in GET) but HTTP response never returns — likely waits for async processing command |
| Embedding not happening | embedded:false, :8081 not running (flag toggled at runtime) |
| :8080/:8081 down | allow_local_processing changed via API, not at launcher startup — needs `onb-stack up` restart |
| Source chat SSE | Error "Could not connect to AI provider" — no default model, local processing off |
| /api/embedding | 404 — wrong path |
| /api/commands | 404 — wrong path |
| Insights | No list endpoint (GET /api/insights 404); only get/delete/save-as-note by ID |
| Flashcard GET by ID | 405 Method Not Allowed |
| history_selection.reason | Still says "deepseek-web browser-use" even when direct PoW path used (cosmetic) |

## ❌ NOT YET TESTED
- PC↔mobile sync (blob push/pull, conflicts)
- Local model chat/embedding (needs stack restart with allow_local_processing)
- allow_local_processing=true actually starting llama/embed servers
- File upload sources (PDF, etc.)
- Webpage/crawl sources (sites)
- .apkg export, CSV import
- Podcast generation
- Frontend vitest/tsc/eslint (done on dev box: clean)
- pytest battery (done on dev box: 2433 pass/10 env-fail)

## 🧠 BRAINSTORM — NEW FEATURES / SERVICES
- Free TTS: piper (already in termux stack), free STT: whisper.cpp
- ntfy push: code present, URL unset → zero footprint (configure OPEN_NOTEBOOK_NOTIFY_URL)
- Multi-profile: ONB_PROFILE exported to guest, termux-stack.sh consumer TBD
- Additional free LLM providers (ollama-compatible), embedding re-ranking
- Hybrid search (text + vector), agentic tools, MCP client
- ModelSelector UI: add `deepseek-web` option gated on pow_solver_available
