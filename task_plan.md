# ON++ Three-Provider Cloud AI Integration — Task Plan

## Goal
Refactor ON++ to support three equal-priority cloud AI providers (DeepSeek-web, Google AI Mode/Gemini 4 Hydrogen, Duck.ai) with user-choice selection, defaulting to deepseek-web. Includes session continuation, attachment handling per provider limits, quota tracking for all providers, mid-conversation provider switching, local processing fallback, UI repolish, and phone/PC independent builds.

## Key Decisions (locked in)

| Area | Decision |
|------|----------|
| Provider priority | All 3 equal; default = deepseek-web |
| Playwright/CDP | NO — lightweight wrappers only (httpx, jsdom for Duck.ai PoW) |
| Local processing | Cloud-first; only when no internet + user approval |
| Duck.ai default model | GPT-6 Luna |
| Duck.ai models | 6 models: gpt-6-luna, claude-haiku-5.5, gpt-5.4-mini/nano, mistral-small-4, gpt-oss-120b, gemma-4-31b |
| Duck.ai model selection | Per-message allowed |
| Model vs provider | Deep refactoring — separate abstraction |
| Session continuation | DeepSeek-web: session series; Google AI: context feeding + session_token; Duck.ai: session series |
| Attachments | Preprocessor feeds context per provider limits; direct upload via provider when supported |
| Quota tracking | Individual + cumulative per model/service; reasoning tokens separate |
| Provider indication | Show which provider handled each message; indicate when switched |
| Reasoning tokens | Separate from response text (collapsible, hidden by default) |
| Editable messages | All 3 AI support |
| Phone vs PC | Independent builds; LAN sync prompt when connected |
| No multi-account | For the 3 AI services |
| Auto-detect availability | Ping intermittently, prompt user on failure |
| Fallback chain | All 3 unavailable → slow local processing |

## Provider Integration Status

### DeepSeek-web ✅ WORKING & VERIFIED
- Auth: Manual credentials (ds_session_id + authorization token from chat.deepseek.com DevTools)
- PoW: Solved via deepseek-pow + wasmtime locally
- Chat: ✅ Verified end-to-end
- Attachments: ✅ Verified end-to-end (20-50 files, 100MB/file)
- Session continuation: ✅ Verified
- Provider status: ✅ Available (creds + PoW solver detected)

### Duck.ai ✅ WORKING & VERIFIED (via revesis/duckai bridge)
- Auth: None (free, no API key)
- Integration: revesis/duckai bridge at http://127.0.0.1:8765/v1 (OpenAI-compatible /v1/chat/completions)
- PoW: Solved via Node.js + jsdom (NO Playwright/CDP needed — browserless)
- Models: gpt-6-luna (default), gpt-5.6-luna, gpt-5.4-nano, gpt-5.4-mini, claude-haiku-4-5, mistral-small-2603
- Bridge: duckai_server.py exposes /health, /v1/models, /v1/chat/completions
- Tested through ON++: ✅ "Duck.ai via ON++ bridge works", ✅ 2-sentence quantum computing explanation
- Note: Direct p2d-duck/duck_ai libraries fail PoW (418 ERR_CHALLENGE) — use the bridge instead

### Google AI Mode ⚠️ CODE COMPLETE, AUTH NEEDED
- Auth: None (free, no API key) BUT requires Google account authentication
- Integration: Two paths:
  1. Local wrapper URL → XD06/google-ai-mode at `http://127.0.0.1:18080/v1` (OpenAI-compatible /v1/chat/completions)
  2. Direct Gemini API → `GEMINI_API_KEY` at `https://generativelanguage.googleapis.com/v1beta/openai/`
- Status: Wrapper URL configurable in Settings, but wrapper server not running
- BLOCKER: Google AI Mode requires Google authentication (browser cookies). Google actively blocks automated access without auth (ORB blocking, QUIC errors).
- Solution: User exports `__Secure-1PSID` + `__Secure-1PSIDTS` cookies from browser (Cookie-Editor), configures XD06/google-ai-mode with cookies.json, starts wrapper at port 18080
- Context: Sliding window (32K free tier), context feeding via preprocessing

## Phases

### Phase 1: Codebase Sanitation — Model vs Provider Separation ✅ COMPLETE
### Phase 2: DeepSeek-web Enhancement ✅ COMPLETE & VERIFIED
### Phase 3: Google AI Mode Integration ✅ CODE COMPLETE, AUTH NEEDED
### Phase 4: Duck.ai Integration ✅ COMPLETE & VERIFIED (via revesis/duckai bridge)
### Phase 5-10: Quota Tracking, Provider Switching, Local Fallback, UI Repolish, Phone/PC, Testing ✅ INFRASTRUCTURE COMPLETE

## Provider Detection API
- `GET /api/providers` — List all providers with metadata
- `GET /api/providers/status` — Dynamic availability with per-provider diagnostics
- DeepSeek-web: checks credentials + PoW solver
- Google AI Mode: checks wrapper URL + Gemini API key
- Duck.ai: checks p2d_duck/duck_ai package + duck_ai_url

## Current State
- PC stack running: API:5055, SurrealDB:8000, Frontend:8502
- DeepSeek-web: Fully configured and verified
- Duck.ai: Fully configured and verified via revesis/duckai bridge at port 8765
- Google AI Mode: URL configured in Settings, wrapper server not running (needs Google cookies)
- All code committed and pushed to plusplus/main (commit 0254a1ff)

## Remaining (require user action)
1. **Google AI Mode**: Run XD06/google-ai-mode wrapper at port 18080 with exported Google browser cookies
2. **Duck.ai**: Already working — no further action needed
3. Test mid-conversation provider switching once Google AI Mode wrapper is available
4. Test attachments with Google AI Mode and Duck.ai once Google AI Mode wrapper is available
