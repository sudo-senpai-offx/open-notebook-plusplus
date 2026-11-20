# ON++ Three-Provider Cloud AI Integration — Task Plan

## Goal
Refactor ON++ to support three equal-priority cloud AI providers (DeepSeek-web, Google AI Mode/Gemini 4 Hydrogen, Duck.ai) with user-choice selection, defaulting to deepseek-web. Includes session continuation, attachment handling per provider limits, quota tracking for all providers, mid-conversation provider switching, local processing fallback, UI repolish, and phone/PC independent builds.

## Key Decisions (from user brainstorming, locked in)

| Area | Decision |
|------|----------|
| Provider priority | All 3 equal; default = deepseek-web |
| Playwright/CDP | NO — lightweight wrappers only |
| Local processing | Cloud-first; only when no internet + user approval |
| Duck.ai default model | GPT-6 Luna |
| Duck.ai models | 6 models: GPT-6 Luna, Claude Haiku 5.5, GPT-5.4 mini/nano, Mistral Small 4, GPT-OSS 120B, Gemma 4 31B |
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

## Provider Limits (from user + research)

| Provider | Max Files | Max Size/File | Context Window | Notes |
|----------|-----------|---------------|----------------|-------|
| DeepSeek-web | 20-50 per chat | 100 MB | 1M tokens (hard lock) | Dynamic daily cap, text extraction 10k-1M tokens |
| Duck.ai | 1 PDF / images | 15-50 pages (free/pro) | Unpublished (daily/weekly quota) | Images+PDFs only, vision-capable model |
| Google AI Mode | 10 per prompt | 100 MB | 32K/128K/1M (free/plus/pro) | Sliding window, Gemini 4 Hydrogen |

## Phases

### Phase 1: Codebase Sanitation — Model vs Provider Separation
- [ ] Refactor `model` vs `provider` distinction across fullstack (frontend types, backend routers, services)
- [ ] Create Provider abstraction layer (base class/interface)
- [ ] Separate model IDs from provider IDs in SurrealDB records
- [ ] Update ModelSelector UI to show provider + model
- [ ] Update all locales (14 files) for new keys
- **Exit criteria**: TypeScript + Python compile cleanly, no model/provider confusion

### Phase 2: DeepSeek-web Enhancement
- [ ] Session continuation: series of sessions when context full (~1M tokens hard lock)
- [ ] Session deletion: delete previous session on chat.deepseek.com when continuing
- [ ] Attachment handling: 20-50 files, 100MB/file, text extraction cap, ref_file_ids
- [ ] Quota tracking: daily cap, dynamic
- [ ] Credentials: manual entry + PC fallback URL (already partially done)
- **Exit criteria**: Chat with attachments works through /api/chat/execute

### Phase 3: Google AI Mode (Gemini 4 Hydrogen) Integration
- [ ] Lightweight wrapper (no Playwright/CDP) — OpenAI-compatible endpoint or curl_cffi
- [ ] Context feeding with preprocessing (sliding window: 32K free / 128K Plus / 1M Pro)
- [ ] Session token continuation for multi-turn
- [ ] 10 files per prompt, 100MB/file
- [ ] Settings: Google AI Mode URL input + save
- [ ] Language support: all Google-supported languages
- **Exit criteria**: Send message via Google AI Mode from ON++ UI

### Phase 4: Duck.ai Integration
- [ ] Lightweight wrapper — p2d-duck (PyPI) or OpenAI-compatible server
- [ ] 6 models: GPT-6 Luna (default), Claude Haiku 5.5, GPT-5.4 mini/nano, Mistral Small 4, GPT-OSS 120B, Gemma 4 31B
- [ ] Model selector per message
- [ ] Session series continuation (like DeepSeek-web)
- [ ] Attachments: Images and PDFs only (15/35/50 page limits)
- [ ] Rate limit: ~20 req/60s, 1s between, daily/weekly quota
- [ ] Settings: Duck.ai URL input + save + model selector
- **Exit criteria**: Send message via Duck.ai from ON++ UI with model selection

### Phase 5: Quota Tracking for All Providers
- [ ] Individual trackers per model/service
- [ ] Reasoning tokens tracked separately
- [ ] Cumulative totals per session and all sessions
- [ ] Ping intermittently, auto-detect availability, prompt user on failure
- [ ] Settings UI: quota display per provider
- **Exit criteria**: Quota dashboard shows all 3 providers + local

### Phase 6: Provider Switching Mid-Conversation
- [ ] Switch among 3 AIs within same chat
- [ ] Context feeding when switching (preprocess full context for new provider)
- [ ] Editable messages supported by all 3 AI
- [ ] Provider indication per message + switch notification
- **Exit criteria**: Switch provider mid-chat with context preserved

### Phase 7: Local Processing Fallback
- [ ] Cloud-first architecture
- [ ] Auto-switch to local when no internet (if `allow_local_processing` enabled + user approval)
- [ ] When disabled: prompt user to allow processing or stop session (resumable when cloud returns)
- [ ] Slow local processing as last resort (enough depth, not starving device)
- **Exit criteria**: Fallback works when all 3 cloud providers unavailable

### Phase 8: UI/UX Repolish
- [ ] Show which provider handled each message
- [ ] Indication when model/provider switched
- [ ] Reasoning tokens separate from response text (collapsible, hidden by default)
- [ ] Provider + model selector in Settings (DeepSeek-web / Google AI Mode / Duck.ai / Auto)
- [ ] DeepSeek settings: PC fallback URL (done)
- [ ] Google AI Mode settings: URL + save
- [ ] Duck.ai settings: URL + save + model selector
- [ ] Frontend repolish (SolidJS/Next.js architecture)
- **Exit criteria**: Frontend compiles, all 3 providers selectable from UI

### Phase 9: Phone vs PC Architecture
- [ ] Independent builds (PC first, then mobile)
- [ ] LAN sync: prompt user whether to sync when connected
[ ] No PC-dependent fallbacks for mobile
- [ ] Lightweight for minimum specs (3GB RAM, 8GB storage, Cortex-A53+)
- [ ] ADB tunnel support (15055→5055, 18502→8502, 28022→8022)
- **Exit criteria**: PC build stable, mobile build planned

### Phase 10: Testing, Verification, Lock-in
- [ ] Real-world testing of all 3 providers
- [ ] Verified by both user and me
- [ ] Pinging during testing for unidentified issues
- [ ] No defects/placeholders
- [ ] Codebase sanitation complete
- [ ] All functions truly verified
- [ ] Commit and push to plusplus/main
- **Exit criteria**: User approves and ships to GitHub

## Current State
- PC stack running: API:5055, SurrealDB:8000, Frontend:8502
- DeepSeek-web credentials configured and working
- Model defaults set: default_chat_model = deepseek-web
- Attachments working through /api/chat/execute with deepseek-web
- SettingsForm.tsx restored with PC fallback URL field
- TypeScript compiles with zero errors
- All changes committed and pushed to plusplus/main (commits 9432642e, d403317f)

## Next Immediate Action
Start Phase 1: Model vs Provider separation refactoring.