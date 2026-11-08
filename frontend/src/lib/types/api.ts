export interface NotebookResponse {
  id: string
  name: string
  description: string
  archived: boolean
  created: string
  updated: string
  source_count: number
  note_count: number
}

export interface NoteResponse {
  id: string
  title: string | null
  content: string | null
  note_type: string | null
  created: string
  updated: string
}

export interface SourceListResponse {
  id: string
  title: string | null
  topics?: string[]                  // Make optional to match Python API
  asset: {
    file_path?: string
    url?: string
  } | null
  embedded: boolean
  embedded_chunks: number            // ADD: From Python API
  insights_count: number
  created: string
  updated: string
  file_available?: boolean
  // ADD: Async processing fields from Python API
  command_id?: string
  status?: string
  processing_info?: Record<string, unknown>
}

export interface SourceDetailResponse extends SourceListResponse {
  full_text: string
  notebooks?: string[]  // List of notebook IDs this source is linked to
}

export type SourceResponse = SourceDetailResponse

export interface SourceStatusResponse {
  status?: string
  message: string
  processing_info?: Record<string, unknown>
  command_id?: string
}

export interface SettingsResponse {
  default_content_processing_engine_doc?: string
  default_content_processing_engine_url?: string
  default_embedding_option?: string
  auto_delete_files?: string
  docling_ocr?: boolean
  docling_formulas?: boolean
  docling_vision?: boolean
  youtube_preferred_languages?: string[]
  allow_local_processing?: boolean
  deepseek_ds_session_id?: string | null
  deepseek_authorization_token?: string | null
  session_id?: string
  local_processing_enabled?: boolean
  cloud_processing_enabled?: boolean
  browser_session_active?: boolean
  last_refresh_at?: string
  rate_limits?: Record<string, unknown>
}

export interface Capabilities {
  docling_available: boolean
  crawl4ai_available: boolean
  crawl4ai_remote_configured: boolean
}

export interface CreateNotebookRequest {
  name: string
  description?: string
}

export interface UpdateNotebookRequest {
  name?: string
  description?: string
  archived?: boolean
}

export interface NotebookDeletePreview {
  notebook_id: string
  notebook_name: string
  note_count: number
  exclusive_source_count: number
  shared_source_count: number
}

export interface NotebookDeleteResponse {
  message: string
  deleted_notes: number
  deleted_sources: number
  unlinked_sources: number
}

export interface CreateNoteRequest {
  title?: string
  content: string
  note_type?: string
  notebook_id?: string
}

export interface CreateSourceRequest {
  // Backward compatibility: support old single notebook_id
  notebook_id?: string
  // New multi-notebook support
  notebooks?: string[]
  // Required fields
  type: 'link' | 'upload' | 'text'
  url?: string
  file_path?: string
  content?: string
  title?: string
  transformations?: string[]
  embed?: boolean
  delete_source?: boolean
  // New async processing support
  async_processing?: boolean
}

export interface UpdateNoteRequest {
  title?: string
  content?: string
  note_type?: string
}

export interface UpdateSourceRequest {
  title?: string
  type?: 'link' | 'upload' | 'text'
  url?: string
  content?: string
}

export interface APIError {
  detail: string
}

// Source Chat Types
// Base session interface with common fields
export interface BaseChatSession {
  id: string
  title: string
  created: string
  updated: string
  message_count?: number
  model_override?: string | null
  transcript_stored?: boolean
  transcript_exported?: boolean
}

export interface HistorySelection {
  kept_exchanges: number
  dropped_exchanges: number
  total_exchanges: number
  reason: string
}

export interface SourceChatSession extends BaseChatSession {
  source_id: string
  model_override?: string
}

export interface SourceChatMessage {
  id: string
  type: 'human' | 'ai'
  content: string
  timestamp?: string
}

export interface SourceChatContextIndicator {
  sources: string[]
  insights: string[]
  notes: string[]
}

export interface SourceChatSessionWithMessages extends SourceChatSession {
  messages: SourceChatMessage[]
  context_indicators?: SourceChatContextIndicator
}

export interface CreateSourceChatSessionRequest {
  source_id: string
  title?: string
  model_override?: string
}

export interface UpdateSourceChatSessionRequest {
  title?: string
  model_override?: string
}

export interface SendMessageRequest {
  message: string
  model_override?: string
  history_since?: string
}

export interface SourceChatStreamEvent {
  type: 'user_message' | 'ai_message' | 'context_indicators' | 'history_selection' | 'complete' | 'error'
  content?: string
  data?: unknown
  message?: string
  timestamp?: string
}

// Notebook Chat Types
export interface NotebookChatSession extends BaseChatSession {
  notebook_id: string
}

export interface NotebookChatMessage {
  id: string
  type: 'human' | 'ai'
  content: string
  timestamp?: string
}

export interface NotebookChatSessionWithMessages extends NotebookChatSession {
  messages: NotebookChatMessage[]
}

export interface CreateNotebookChatSessionRequest {
  notebook_id: string
  title?: string
  model_override?: string
}

export interface UpdateNotebookChatSessionRequest {
  title?: string
  model_override?: string | null
}

export interface SendNotebookChatMessageRequest {
  session_id: string
  message: string
  context: {
    sources: Array<Record<string, unknown>>
    notes: Array<Record<string, unknown>>
  }
  model_override?: string
  history_since?: string
}

export interface SendNotebookChatMessageResponse {
  session_id: string
  messages: NotebookChatMessage[]
  history_selection?: HistorySelection
}

export interface BuildContextRequest {
  notebook_id: string
  context_config: {
    sources: Record<string, string>
    notes: Record<string, string>
  }
  // The user's question. Sent at send time so the server can retrieve the
  // passages that match it. Omitted by callers that only want a size
  // estimate, such as the podcast generator.
  query?: string
  retrieval_limit?: number
}

export interface BuildContextResponse {
  context: {
    sources: Array<Record<string, unknown>>
    notes: Array<Record<string, unknown>>
  }
  token_count: number
  char_count: number
  // Present only when a query was sent. `mode` is "retrieval" when the
  // context came from matched passages, "documents" when it fell back to
  // whole sources and notes.
  retrieval?: {
    mode: 'retrieval' | 'empty' | 'fallback' | 'documents'
    chunks: number
    documents: number
    characters: number
    dropped_out_of_scope?: number
    dropped_over_budget?: number
    reason?: string
  }
}

export interface RecentlyViewedResponse {
  type: 'notebook' | 'source'
  id: string
  title: string
  last_viewed_at: string
}

// DeepSeek-web API types
export interface DeepSeekSessionStatus {
  credentials_configured: boolean
  manual_login_required: boolean
  rate_limited: boolean
  pow_required: boolean
  pow_solver_available: boolean
}

export interface DeepSeekConfigureRequest {
  ds_session_id: string
  authorization_token: string
}

export interface DeepSeekConfigureResponse {
  ok: boolean
  message: string
}

export interface DeepSeekRefreshResponse {
  ok: boolean
  message: string
}

export interface DeepSeekAvailableResponse {
  internet_available: boolean
  credentials_configured: boolean
  ready: boolean
}

export interface DeepSeekChatRequest {
  message: string
  session_id?: string
  notebook_id?: string
  message_type?: string
  thinking_enabled?: boolean
}

export interface DeepSeekChatResponse {
  response: string | null
  error: string | null
  manual_login_required: boolean
  rate_limited: boolean
  pow_required: boolean
  session_id: string | null
  prompt_used: string | null
}

// TTS/STT API types (D14/D15)
export interface TTSRequest {
  text: string
  voice?: string | null
  language?: string | null
  out_path?: string | null
}

export interface TTSResponse {
  ok: boolean
  audio_path?: string | null
  bytes_written?: number
  provider?: string | null
  language?: string | null
  voice?: string | null
  error?: string | null
  browser_fallback?: boolean
}

export interface STTRequest {
  language?: string | null
}

export interface STTResponse {
  ok: boolean
  text?: string | null
  seconds?: number
  provider?: string | null
  language?: string | null
  error?: string | null
  browser_fallback?: boolean
}

export interface VoiceList {
  piper: string[]
  edge_tts: string[]
  browser_web_speech: string[]
}

export interface SupportedLanguages {
  supported_large_tier: string[]
  supported_small_tier: string[]
  unsupported: string[]
}

// Preprocessor API types (B6-B9)
export interface PreprocessRequest {
  paths: string[]
  settings?: Record<string, boolean>
  language?: string | null
}

export interface PreprocessResult {
  ok: boolean
  source_path: string
  markdown_path?: string | null
  markdown_content?: string | null
  file_group?: string | null
  force_enabled?: boolean
  error?: string | null
}

export interface PreprocessResponse {
  results: PreprocessResult[]
}

export interface PreprocessGroups {
  groups: Record<string, boolean>
}

// Prompt Library API types (G24/G25)
export interface PromptInfo {
  name: string
  description: string
  variables: string[]
}

export interface PromptRenderRequest {
  variables: Record<string, unknown>
}

export interface PromptRenderResponse {
  prompt: string
  rendered: string
}

// Browser AI API types (D17)
export interface GoogleAIModeRequest {
  prompt: string
  session_token?: string | null
  country?: string
  language?: string
}

export interface GoogleAIModeResponse {
  ok: boolean
  response?: string | null
  session_token?: string | null
  provider: string
  error?: string | null
  rate_limited?: boolean
}

export interface DuckAIRequest {
  prompt: string
  model?: string | null
}

export interface DuckAIResponse {
  ok: boolean
  response?: string | null
  provider: string
  error?: string | null
  rate_limited?: boolean
}

export interface BrowserAIStatus {
  google_ai_mode: {
    configured: boolean
    url?: string | null
    playwright_fallback: boolean
  }
  duck_ai: {
    configured: boolean
    url?: string | null
    p2d_duck_available: boolean
  }
  rate_limits: {
    duck_ai: string
    google_ai_mode: string
  }
}

// Flashcard generation request
export interface FlashcardGenerateRequest {
  notebook_id: string
  source_id?: string | null
  source_text: string
  language: string
  card_type: 'basic' | 'cloze'
  count: number
}

export interface FlashcardGenerateResponse {
  ok: boolean
  cards: Array<{
    id?: string
    guid?: string
    model?: string
    front?: string | null
    back?: string | null
    cloze_text?: string | null
    tags?: string[]
    source_id?: string | null
    status?: string
  }>
  count: number
  error?: string | null
}

// Podcast script generation request
export interface PodcastScriptRequest {
  notebook_id: string
  source_text: string
  tone?: string
  duration?: number
  speakers?: string
}

export interface PodcastScriptResponse {
  ok: boolean
  script?: string | null
  error?: string | null
}

// Synthetic data generation request
export interface SyntheticDataRequest {
  notebook_id: string
  topic: string
  difficulty?: number
  count?: number
}

export interface SyntheticDataResponse {
  ok: boolean
  pairs: Array<{ instruction: string; response: string }>
  count: number
  error?: string | null
}

// Transcript post-processing request
export interface TranscriptPostProcessRequest {
  transcript: string
  notebook_id: string
  language?: string
}

export interface TranscriptPostProcessResponse {
  ok: boolean
  cleaned_transcript?: string | null
  original_length?: number
  cleaned_length?: number
  error?: string | null
}
