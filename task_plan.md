# ON++ Three-Provider Cloud AI Integration — Task Plan

## Goal
Refactor ON++ to support three equal-priority cloud AI providers (DeepSeek-web, Google AI Mode/Gemini 4 Hydrogen, Duck.ai) with user-choice selection, defaulting to deepseek-web. Includes session continuation, attachment handling per provider limits, quota tracking for all providers, mid-conversation provider switching, local processing fallback, UI repolish, and phone/PC independent builds.

## Key Decisions (locked in)

| Area | Decision |
|------|----------|
| Provider priority | All 3 equal; default = deepseek-web |
| Playwright/CDP | NO — lightweight wrappers only (httpx, p2d-duck/duck_ai) |
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

### DeepSeek-web ✅ WORKING
- Auth: Manual credentials (ds_session_id + authorization token from chat.deepseek.com DevTools)
- PoW: Solved via deepseek-pow + wasmtime locally
- Chat: ✅ Verified end-to-end
- Attachments: ✅ Verified end-to-end (20-50 files, 100MB/file)
- Session continuation: ✅ Verified
- Provider status: ✅ Available (creds + PoW solver detected)

### Google AI Mode ⚠️ INFRASTRUCTURE NEEDED
- Auth: None (free browser AI, no API key)
- Integration: Two paths:
  1. Local wrapper URL → XD06/google-ai-mode at `http://127.0.0.1:18080/v1` (OpenAI-compatible /v1/chat/completions)
  2. Direct Gemini API → `GEMINI_API_KEY` at `https://generativelanguage.googleapis.com/v1beta/openai/`
- Status: Wrapper URL configurable in Settings, but wrapper server not running. Requires browser cookies from logged-in Google session.
- Context: Sliding window (32K free tier), context feeding via preprocessing

### Duck.ai ⚠️ INFRASTRUCTURE NEEDED
- Auth: None (free anonymous AI, no API key)
- Integration: Two paths:
  1. p2d-duck / duck_ai library — ❌ BLOCKED by PoW challenge (418 ERR_CHALLENGE)
  2. OpenAI-compatible wrapper → pavnxet/duckai-api at `http://localhost:3006/v1` (requires Playwright browser)
- Status: Provider code written and registered. Proxy server can be run but Duck.ai anti-bot measures block token flow without JavaScript execution.
- Models: gpt-6-luna (default), claude-haiku-5.5, gpt-5.4-mini, mistral-small-4, gpt-oss-120b, gemma-4-31b

## Phases

### Phase 1: Codebase Sanitation — Model vs Provider Separation ✅ COMPLETE
- Provider abstraction layer (providers.py, provider_registry.py)
- Provider implementations for all 3 cloud providers
- TypeScript + Python compile cleanly

### Phase 2: DeepSeek-web Enhancement ✅ COMPLETE & VERIFIED
- Credentials, PoW solving, chat, attachments, session continuation

### Phase 3: Google AI Mode Integration ✅ CODE COMPLETE, INFRASTRUCTURE NEEDED
- Provider code written, endpoint fixed to match XD06/google-ai-mode
- NEEDS: XD06/google-ai-mode wrapper at port 18080 with browser cookies

### Phase 4: Duck.ai Integration ✅ CODE COMPLETE, INFRASTRUCTURE NEEDED
- Provider code written, supports both p2d-duck and duck_ai libraries
- NEEDS: JavaScript execution (Playwright) for PoW challenge — intentionally avoided per user decision
- OR: pavnxet/duckai-api proxy at port 3006 (Duck.ai anti-bot currently blocks tokens)

### Phase 5-10: Quota Tracking, Provider Switching, Local Fallback, UI Repolish, Phone/PC, Testing ✅ INFRASTRUCTURE COMPLETE
- All code written and committed
- Testing pending on Google AI Mode and Duck.ai proxy availability

## Provider Detection API
- `GET /api/providers` — List all providers with metadata
- `GET /api/providers/status` — Dynamic availability with per-provider diagnostics
- DeepSeek-web: checks credentials + PoW solver
- Google AI Mode: checks wrapper URL + Gemini API key
- Duck.ai: checks p2d_duck/duck_ai package + duck_ai_url
- Ollama: placeholder (not registered)

## Current State
- PC stack running: API:5055, SurrealDB:8000, Frontend:8502
- DeepSeek-web: Fully configured and verified
- Google AI Mode: URL configured in Settings, wrapper server not running
- Duck.ai: URL configured in Settings, package installed, PoW blocked
- All code committed and pushed to plusplus/main (commit 1eb18344)

## Remaining (require user action)
1. Run XD06/google-ai-mode wrapper at port 18080 (requires Google browser cookies)
2. Duck.ai requires JavaScript execution — direct access blocked by PoW challenge
3. Test mid-conversation provider switching once proxies available
4. Test attachments with Google AI Mode and Duck.ai once proxies available