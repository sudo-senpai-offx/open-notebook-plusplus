# ON++ Three-Provider Cloud AI Integration — Task Plan

## Goal
Refactor ON++ to support three equal-priority cloud AI providers (DeepSeek-web, Google AI Mode/Gemini 4 Hydrogen, Duck.ai) with user-choice selection, defaulting to deepseek-web. Includes session continuation, attachment handling per provider limits, quota tracking for all providers, mid-conversation provider switching, local processing fallback, UI repolish, and phone/PC independent builds.

## Key Decisions (locked in)

| Area | Decision |
|------|----------|
| Provider priority | All 3 equal; default = deepseek-web |
| Playwright/CDP | NO — lightweight wrappers only |
| Local processing | Cloud-first; only when no internet + user approval |
| Duck.ai default model | GPT-6 Luna |
| Duck.ai models | 6 models: gpt-6-luna, claude-haiku-5.5, gpt-5.4-mini/nano, mistral-small-4, gpt-oss-120b, gemma-4-31b |
| Duck.ai model selection | Per-message allowed |
| Model vs provider | Deep refactoring — separate abstraction |
| Session continuation | DeepSeek-web: session series; Google AI: context feeding + session_token; Duck.ai: session series |
| Attachments | Preprocessor feeds context per provider limits; priority attachments bypass preprocessing |
| Quota tracking | Individual + cumulative per model/service; reasoning tokens separate |
| Provider indication | Show which provider handled each message; indicate when switched |
| Reasoning tokens | Separate from response text |
| Editable messages | All 3 AI support |
| Phone vs PC | Independent builds; LAN sync prompt when connected |
| No multi-account | For the 3 AI services |
| Auto-detect availability | Ping intermittently, prompt user on failure |
| Fallback chain | All 3 unavailable → slow local processing |

## Provider Limits

| Provider | Max Files | Max Size/File | Context Window | File Types | Quota |
|----------|-----------|---------------|----------------|------------|-------|
| DeepSeek-web | 20-50 per chat | 100 MB | 1M tokens (hard lock) | Text + images via ref_file_ids | Dynamic daily cap |
| Duck.ai | 1 PDF / images | 15-50 pages | Unpublished | Images + PDFs only | Daily/weekly (UTC midnight) |
| Google AI Mode | 10 per prompt | 100 MB | 32K/128K/1M (free/plus/pro) | Documents, spreadsheets, text, images, audio, video | Refreshes every 5 hours |

## Phases (COMPLETED)

### Phase 1: Codebase Sanitation — Model vs Provider Separation ✅
- Refactored model vs provider distinction across fullstack
- Created Provider abstraction layer (providers.py, provider_registry.py)
- Separated model IDs from provider IDs in SurrealDB records
- Updated ModelSelector UI to show provider + model
- Updated all 14 locale files for new keys
- TypeScript + Python compile cleanly

### Phase 2: DeepSeek-web Enhancement ✅
- Session continuation: fresh session each time (avoids stale session IDs)
- Session deletion: handled by user in DeepSeek web UI
- Attachment handling: 20-50 files, 100MB/file, text extraction cap, ref_file_ids
- Quota tracking: daily cap, dynamic
- Credentials: manual entry + PC fallback URL (working)
- Verified: chat with attachments works end-to-end

### Phase 3: Google AI Mode Integration ✅ (infrastructure)
- Lightweight wrapper support (GOOGLE_AI_MODE_URL) — no Playwright/CDP
- Direct Gemini API support (GEMINI_API_KEY) via OpenAI-compatible endpoint
- Context feeding with preprocessing (sliding window: 32K free / 128K Plus / 1M Pro)
- Session token continuation for multi-turn
- 10 files per prompt, 100MB/file
- Settings: Google AI Mode URL + Gemini API Key input + save
- Language support: all Google-supported languages
- NEEDS: GEMINI_API_KEY env var or GOOGLE_AI_MODE_URL to test

### Phase 4: Duck.ai Integration ✅ (infrastructure)
- Lightweight wrapper support (DUCK_AI_URL) or p2d-duck PyPI client
- 6 models: GPT-6 Luna (default), Claude Haiku 5.5, GPT-5.4 mini/nano, Mistral Small 4, GPT-OSS 120B, Gemma 4 31B
- Model selector per message
- Session series continuation (like DeepSeek-web)
- Attachments: Images and PDFs only (15/35/50 page limits)
- Rate limit: ~20 req/60s, 1s between, daily/weekly quota
- Settings: Duck.ai URL + default model selector + save
- NEEDS: DUCK_AI_URL proxy server (p2d-duck PoW challenge requires JS execution)

### Phase 5: Quota Tracking for All Providers ✅ (infrastructure)
- Individual trackers per model/service
- Reasoning tokens tracked separately
- Cumulative totals per session and all sessions
- Settings UI: quota display per provider

### Phase 6: Provider Switching Mid-Conversation ✅ (infrastructure)
- Switch among 3 AIs within same chat
- Context feeding when switching (preprocess full context for new provider)
- Editable messages supported by all 3 AI
- Provider indication per message + switch notification

### Phase 7: Local Processing Fallback ✅ (infrastructure)
- Cloud-first architecture
- Auto-switch to local when no internet (if allow_local_processing enabled + user approval)
- When disabled: prompt user to allow processing or stop session (resumable when cloud returns)
- Slow local processing as last resort

### Phase 8: UI/UX Repolish ✅
- Show which provider handled each message
- Indication when model/provider switched
- Reasoning tokens separate from response text (collapsible, hidden by default)
- Provider + model selector in Settings (DeepSeek-web / Google AI Mode / Duck.ai / Auto)
- DeepSeek settings: PC fallback URL
- Google AI Mode settings: URL + Gemini API Key + save
- Duck.ai settings: URL + model selector + save
- Frontend repolish complete

### Phase 9: Phone vs PC Architecture ✅ (infrastructure)
- Independent builds (PC first, then mobile)
- LAN sync: prompt user whether to sync when connected
- No PC-dependent fallbacks for mobile
- Lightweight for minimum specs (3GB RAM, 8GB storage, Cortex-A53+)
- ADB tunnel support (15055→5055, 18502→8502, 28022→8022)

### Phase 10: Testing, Verification, Lock-in
- DeepSeek-web: TESTED AND WORKING ✅
- Google AI Mode: NEEDS GEMINI_API_KEY or GOOGLE_AI_MODE_URL
- Duck.ai: NEEDS DUCK_AI_URL proxy server (PoW challenge blocks direct access)
- All changes committed and pushed to plusplus/main

## Current State
- PC stack running: API:5055, SurrealDB:8000, Frontend:8502
- DeepSeek-web credentials configured and working
- Model defaults set: default_chat_model = deepseek-web
- Attachments working through /api/chat/execute with deepseek-web
- SettingsForm.tsx with Google AI Mode + Duck.ai cards
- TypeScript compiles with zero errors
- All changes committed and pushed to plusplus/main (commit 5cd5381f)

## Blocked Items (require user action)
1. **Google AI Mode**: Set GEMINI_API_KEY environment variable OR run XD06/google-ai-mode wrapper at GOOGLE_AI_MODE_URL
2. **Duck.ai**: Run a Duck.ai proxy server at DUCK_AI_URL (p2d-duck PoW challenge requires JavaScript execution)
3. Both are infrastructure requirements, not code issues

## Next Immediate Action
User needs to set GEMINI_API_KEY or DUCK_AI_URL environment variables, then test the corresponding providers end-to-end.