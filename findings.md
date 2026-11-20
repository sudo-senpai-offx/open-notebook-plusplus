# Research Findings — Three-Provider Cloud AI Integration

## Provider 1: DeepSeek-web (Already Working)

### Authentication
- `ds_session_id` cookie from `chat.deepseek.com` DevTools Network tab
- `authorization` Bearer token from request headers
- Stored in `ProcessingFlags` (SurrealDB: `open_notebook:processing_flags`)
- Credentials expire frequently (minutes to hours); requires manual re-login

### PoW Solving
- `DeepSeekHashV1` challenge solved via `deepseek-pow` + `wasmtime`
- Solver installed on host and container
- `pow_solver_available()` checks import of `deepseek_pow` and `wasmtime`

### Attachment Limits
- 20-50 files per chat session (depending on traffic)
- 100 MB max per file
- Message prompt box: ~10-24k tokens (excluding attachments/clipboards)
- Attachment text extraction: ~10k-1M tokens depending on throttling
- Dynamic daily cap
- Supported: text files (prepended as content), images via `ref_file_ids`

### Session Management
- DeepSeek-web retains history on its own web UI (chat.deepseek.com)
- Limited session context length (~1M tokens hard lock)
- Session continuation: series of sessions when context full
- Delete previous session on chat.deepseek.com when continuing
- `chat_session:<uuid>` prefix stripped before sending to DeepSeek

### References
- https://github.com/kittors/deepseek-web-api (architecture docs)
- https://github.com/smkttl/deepseek-api (reverse-engineered web interface)
- https://api-docs.deepseek.com/api/create-chat-completion/ (official API)

## Provider 2: Google AI Mode (Gemini 4 Hydrogen)

### Model
- Gemini 4 Hydrogen: 1M unified token context window (input + output)
- Long decode up to 1M tokens
- Free tier available on Gemini API
- Pricing: $1.50/M input, $7.50/M output (flat across full context)
- Available on Google AI Studio, Gemini app, Google Search, Gemini API

### Access Methods
1. **Gemini Web App** (gemini.google.com): Free tier 32K context, Plus 128K, Pro/Ultra 1M. Max 10 files per prompt, 100MB per file. Sliding window — older turns drop out when limit exceeded.
2. **Chrome Built-in AI** (Gemini Nano / Prompt API): Local, 9,216 token context, 41% eligibility, 6x slower than cloud
3. **Google AI Mode** (search.google.com/ways-to-search/ai-mode): Guest, US-only, 18+, Web & App Activity enabled. No account required.
4. **XD06/google-ai-mode**: Local OpenAI-compatible API wrapper, curl_cffi fast path + Chrome CDP fallback (USER SAID NO TO PLAYWRIGHT/CDP)
5. **Dark2C/Google-AI-Mode-API-Wrapper**: Express + Playwright + Chromium (USER SAID NO TO PLAYWRIGHT/CDP)

### Session Management
- `session_token` for continuation (supported in v1beta1 API)
- When browser closed/restarted: anonymous session is gone
- Continuation: new search with AI feeding (continuation prompts with all past contexts baked in)
- Context feeding needed: models have limited context length, may fail to retain earlier turns

### Rate Limits
- Free tier: limits refresh every 5 hours until weekly limit reached
- More permissive than Duck.ai but still rate-limited

### References
- https://aistudio.google.com/docs/models/gemini-4-hydrogen
- https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-hydrogen
- https://developers.google.com/edge/litert-lm/cli/openai_server (OpenAI-compatible local server)
- https://discuss.ai.google.dev/t/openai-compatible-endpoint-changed/51842

## Provider 3: Duck.ai (DuckDuckGo)

### Models (6 total)
1. GPT-6 Luna (DEFAULT) — OpenAI flagship, high speed
2. Claude Haiku 5.5 — Anthropic, fast concise reasoning
3. GPT-5.4 mini/nano — OpenAI lightweight
4. Mistral Small 4 — Mistral AI multilingual
5. GPT-OSS 120B — Tinfoil massive open-weights
6. Gemma 4 31B — Google Gemma 4 open model (Beta)

### Authentication
- None required (anonymous guest)
- No account needed
- Non-persistent: sessions lost when browser closed/restarted

### Attachment Limits
- Images and PDFs only (vision/document-capable model required)
- Spreadsheets unsupported
- PDF Page Limits: Free 15 pages, Plus 35 pages, Pro 50 pages
- Session context & quota: rolling daily/weekly compute quota
- Resets daily at midnight UTC, weekly Sunday at midnight UTC
- Long multi-turn chats consume quota rapidly (full history resent every turn)

### Rate Limits
- ~20 requests per 60 seconds
- 1 second between requests
- Single active request at a time
- Daily anonymous limit

### Integration Options
1. **p2d-duck** (PyPI): Python 3.10+, deps: httpx, py-mini-racer, cryptography, html5lib. Sync client, auto-retry on challenge failures. No API key.
2. **amirkabiri/duckai**: OpenAI-compatible API server
3. **pavnxet/duckai-api**: Playwright-based (USER SAID NO TO PLAYWRIGHT)
4. **Brioch/duck-ai-api**: Node.js CLI
5. **r0-negative/duckai-api**: Lightweight Python client

### References
- https://pypi.org/project/p2d-duck/
- https://github.com/pavnxet/duckai-api
- https://github.com/Brioch/duck-ai-api
- https://github.com/r0-negative/duckai-api
- https://duckduckgo.com/duckduckgo-help-pages/duckai/chat-models

## Provider Limits Comparison

| Provider | Max Files | Max Size/File | Context Window | File Types | Quota |
|----------|-----------|---------------|----------------|------------|-------|
| DeepSeek-web | 20-50 per chat | 100 MB | 1M tokens (hard lock) | Text + images via ref_file_ids | Dynamic daily cap |
| Duck.ai | 1 PDF / images | 15-50 pages | Unpublished | Images + PDFs only | Daily/weekly (UTC midnight) |
| Google AI Mode | 10 per prompt | 100 MB | 32K/128K/1M (free/plus/pro) | Documents, spreadsheets, text, images, audio, video | Refreshes every 5 hours |

## ON++ Architecture Decisions (from user)

- All 3 providers equal priority, default = deepseek-web
- No Playwright/CDP — lightweight wrappers only
- Cloud-first; local fallback only when no internet + user approval
- Duck.ai default model: GPT-6 Luna
- Per-message model selection for Duck.ai
- Session continuation: DeepSeek-web (session series), Google AI (context feeding + session_token), Duck.ai (session series)
- ON++ remembers full context for all providers; preprocesses/feeds context per provider limits
- Priority attachments bypass preprocessing but still respect individual AI limits
- Quota tracker: individual + cumulative per model/service; reasoning tokens separate
- Provider indication per message; indication when switched
- Reasoning tokens separate from response text
- Editable messages: all 3 AI support
- Phone vs PC: independent builds; LAN sync prompt when connected
- No PC-dependent fallbacks for mobile
- Minimum device baseline: 3GB RAM, 8GB free storage, Cortex-A53+

## Current Codebase State (as of session start)

- PC stack: API:5055 (uvicorn), SurrealDB:8000 (native v2.7.0), Frontend:8502 (Next.js)
- DeepSeek-web: credentials configured, chat tested successfully
- Attachments: working through /api/chat/execute with deepseek-web
- SettingsForm.tsx: restored with PC fallback URL field
- TypeScript: zero errors
- Python: syntax checks pass
- All changes committed and pushed to plusplus/main (commits 9432642e, d403317f)

## Files Modified in Current Session

1. `frontend/src/app/(dashboard)/settings/components/SettingsForm.tsx` — Restored from git HEAD, added PC fallback URL field
2. `frontend/src/lib/api/settings.ts` — Restored missing API exports (sessionApi, quotaApi, attachmentApi, chatApi, dynamicCourseApi, syncApi), added TTS types
3. `frontend/src/lib/types/api.ts` — Added TTSRequest/TTSResponse, fixed duplicate error field, added missing types
4. `api/routers/chat.py` — Replaced deepseek_session_service with deepseek_direct_service for deepseek-web execute path
5. `open_notebook/services/deepseek_session_service.py` — Configure credentials from ProcessingFlags before each send, fresh session each time
6. `task_plan.md` — Created (this plan)
7. `findings.md` — Created (this file)
8. `progress.md` — Created (session log)