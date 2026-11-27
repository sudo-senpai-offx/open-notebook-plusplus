export interface NotebookResponse {
  id: string
  name: string
  description: string
  archived: boolean
  created: string
  updated: string
  source_count: number
  note_count: number
  // Study OS course classification (Phase 2)
  field?: string
  expected_completion_duration?: number
  curated?: boolean
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
  // Extraction observability (Week 5 Day 5 / Week 6 Day 1)
  warnings?: string[]
  timed_out_pages?: number[]
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
  field?: string
  expected_completion_duration?: number
  curated?: boolean
}

export interface UpdateNotebookRequest {
  name?: string
  description?: string
  archived?: boolean
  field?: string
  expected_completion_duration?: number
  curated?: boolean
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
  thinking?: string | null
  provider_id?: string | null
  model_name?: string | null
  edit_count?: number
  original_content?: string | null
  edited_at?: string | null
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
  thinking?: string | null
  edit_count?: number
  original_content?: string | null
  edited_at?: string | null
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
  attachments?: string[]
}

export interface SendNotebookChatMessageResponse {
  session_id: string
  messages: NotebookChatMessage[]
  history_selection?: HistorySelection
  // Provider and model info for frontend display
  provider_id?: string | null
  model_name?: string | null
  // Reasoning message - separate from response for collapsible UI
  reasoning?: string | null
  // Fallback notification for cloud/local switching
  local_fallback_used?: boolean
  cloud_exhausted?: boolean
  switch_to_cloud_suggested?: boolean
  fallback_notification?: {
    type: string
    message: string
    actions: string[]
  } | null
}


export interface EditMessageRequest {
  message_id: string
  new_content: string
  new_attachments?: string[]
}

export interface EditMessageResponse {
  session_id: string
  messages: SourceChatMessage[]
  edit_count: number
  continuations_deleted: number
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

export interface ProcessingFlagsResponse {
  allow_local_processing: boolean
  deepseek_pc_url: string | null
  google_ai_mode_url: string | null
  lightpanda_cdp_url: string | null
  gemini_api_key: string | null
  duck_ai_url: string | null
  duck_ai_default_model: string | null
  ollama_url: string | null
  ollama_default_model: string | null
  // Session-level configuration
  session_cloud_local_switching: boolean
  session_auto_compaction_threshold: number
  session_auto_compaction_hard_limit: number
  session_high_priority_timeout_days: number
  session_lazy_load_enabled: boolean
  // Context preprocessor per-group toggles
  preprocess_documents: boolean
  preprocess_audio: boolean
  preprocess_image: boolean
  preprocess_video: boolean
  preprocess_force_enabled: boolean
  // Sync configuration
  sync_dir: string | null
  sync_token: string | null
  sync_paired_devices: string[]
  sync_auto_sync: boolean
  sync_userspace_only: boolean
  sync_notebooks: boolean
  sync_sessions: boolean
  sync_analytics_enabled: boolean
  sync_conflict_resolution: string
  sync_last_sync_at: string | null
}

export interface SessionStatusResponse {
  session_id?: string
  local_processing_enabled?: boolean
  cloud_processing_enabled?: boolean
  browser_session_active?: boolean
  last_refresh_at?: string
  rate_limits?: Record<string, unknown>
  allow_local_processing?: boolean
}

export interface QuotaStatusResponse {
  providers: Record<string, { used: number; limit: number; remaining: number; exceeded: boolean; reset_at?: string; last_used_at?: string }>
  cumulative_used: number
  cumulative_limit: number
  cumulative_remaining: number
  cumulative_exceeded: boolean
  updated_at?: string
}

export interface ChatBuildContextRequest {
  session_id: string
  notebook_id?: string
  message: string
}

export interface ChatBuildContextResponse {
  context: string
}

export interface ChatEditMessageRequest {
  message_id: string
  content?: string
  new_content?: string
}

export interface ChatEditMessageResponse {
  ok: boolean
}

export interface DynamicCourseRequest {
  topic: string
  field: string
  estimated_duration_days?: number | null
  user_preferences?: string | null
}

export interface SyncConflictsResponse {
  conflicts: SyncConflictResponse[]
}

export interface SyncConflictResponse {
  collection: string
  db_record_id: string
  local_updated?: string | null
  remote_updated?: string | null
  resolved: boolean
  resolution?: string | null
}

export interface SyncOptoutResponse {
  collections: string[]
  record_ids: string[]
}

export interface SyncResolveResponse {
  ok: boolean
}

export interface SyncUpdateOptoutResponse {
  ok: boolean
}

export interface SyncAutoSyncResponse {
  ok: boolean
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
  attachments?: string[]
}

export interface DeepSeekChatResponse {
  response: string | null
  error: string | null
  manual_login_required: boolean
  rate_limited: boolean
  pow_required: boolean
  session_id: string | null
  prompt_used: string | null
  provider?: string | null
  language?: string | null
  voice?: string | null
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

// Study OS — Assessments (Phase 3)
export interface AssessmentCreate {
  notebook_id: string
  field: string
  type: 'quiz' | 'exam' | 'mock'
  score: number
  total_questions?: number
  correct_answers?: number
  notes?: string
}

export interface AssessmentResponse {
  id: string
  notebook_id: string
  field: string
  type: string
  score: number
  total_questions?: number | null
  correct_answers?: number | null
  notes?: string | null
  created_at: string
}

export interface SpiderwebMetric {
  field: string
  score: number
  quiz: number
  exam: number
  reflection: number
}

export interface SpiderwebMetricsResponse {
  fields: SpiderwebMetric[]
}

// Study OS — Timetable (Phase 3)
export interface TimetableBlockCreate {
  notebook_id?: string | null
  title: string
  activity_type: 'study' | 'flashcard' | 'assessment' | 'break'
  field?: string | null
  start_time: string
  end_time: string
  is_high_intensity: boolean
}

export interface TimetableBlockResponse {
  id: string
  notebook_id?: string | null
  title: string
  activity_type: string
  field?: string | null
  start_time: string
  end_time: string
  is_high_intensity: boolean
  created_at: string
}

export interface BurnoutCheckResponse {
  date: string
  high_intensity_hours: number
  overloaded: boolean
  limit_hours: number
  message: string
}

// Study OS — Dynamic Course Creation (Phase 3)
export interface DynamicCourseCreate {
  topic: string
  field: string
  estimated_duration_days?: number | null
  user_preferences?: string | null
}

export interface DynamicCourseModule {
  module_title: string
  module_description: string
  estimated_hours: number
  lessons: string[]
}

export interface DynamicCourseResponse {
  notebook_id: string
  topic: string
  field: string
  estimated_duration_days: number
  modules: DynamicCourseModule[]
  exam_mock_topics: string[]
}

// ============================================================================
// Course Generation Pipeline Types
// ============================================================================

export type CourseStatus = 'draft' | 'in_review' | 'published' | 'archived'
export type DraftStatus = 'draft' | 'approved' | 'abandoned' | 'superseded'
export type BlockType =
  | 'source_grounded'
  | 'transformation_derived'
  | 'ai_generated'
  | 'research_gap'
  | 'social_quote'

export interface BloomMix {
  remember: number
  understand: number
  apply: number
  analyze: number
  evaluate: number
  create: number
}

export interface CourseObjectiveResponse {
  id: string
  course_id: string
  title: string
  description: string
  bloom_level: string
  bloom_verb: string
  condition: string | null
  criterion: string | null
  order_index: number
  created: string | null
  updated: string | null
}

export interface CourseLessonResponse {
  id: string
  course_id: string
  module_title: string
  title: string
  content: string
  order_index: number
  prerequisite_lesson_ids: string[]
  objective_ids: string[]
  block_type: BlockType
  source_id: string | null
  transformation_id: string | null
  flashcard_ids: string[]
  remedial_branch: string | null
  ai_generated: boolean
  narrator_voice_id: string | null
  created: string | null
  updated: string | null
}

export interface CourseAssessmentResponse {
  id: string
  course_id: string
  lesson_id: string | null
  objective_id: string
  item_type: string
  question: string
  answer: string
  options: string[]
  bloom_level: string
  points: number
  order_index: number
  created: string | null
  updated: string | null
}

export interface CourseEnrollmentResponse {
  id: string
  course_id: string
  user_id: string
  status: string
  current_lesson_id: string | null
  completed_lesson_ids: string[]
  passed_assessment_ids: string[]
  completion_percent: number
  started_at: string | null
  completed_at: string | null
  updated: string | null
}

export interface CourseResponse {
  id: string
  title: string
  description: string | null
  notebook_id: string
  status: CourseStatus
  field: string | null
  estimated_duration_days: number | null
  narrator_voice_id: string | null
  narrator_speed: number
  grounding_minimum_percent: number
  bloom_mix: Record<string, number>
  created: string | null
  updated: string | null
}

export interface CreateCourseRequest {
  title: string
  description?: string
  notebook_id: string
  field?: string
  estimated_duration_days?: number | null
  bloom_mix?: Record<string, number>
  grounding_minimum_percent?: number
  narrator_voice_id?: string | null
  narrator_speed?: number
}

export interface UpdateCourseRequest {
  title?: string
  description?: string
  field?: string
  estimated_duration_days?: number | null
  narrator_voice_id?: string | null
  narrator_speed?: number
  grounding_minimum_percent?: number
  bloom_mix?: Record<string, number>
  status?: CourseStatus
}

export interface CreateDraftRequest {
  course_id?: string | null
  topic: string
  audience: string
  prior_knowledge: string
  bloom_mix?: Record<string, number>
  estimated_duration_days?: number | null
  notebook_id?: string | null
  model_tier?: string
  token_budget?: number
}

export interface DraftResponse {
  id: string
  course_id: string | null
  stage: string
  status: DraftStatus
  version: number
  payload: Record<string, unknown>
  alignment_report: Record<string, unknown>
  created: string | null
  updated: string | null
}

export interface DraftAdvanceRequest {
  model_tier?: string | null
  token_budget?: number | null
}

export interface DraftApproveRequest {
  checkpoint: string
  approved: boolean
  feedback?: string | null
}

export interface ExportRequest {
  format: 'scorm12' | 'scorm2004' | 'pdf' | 'docx' | 'html' | 'markdown'
}

export interface EnrollRequest {
  user_id: string
}

export interface AlignmentDefect {
  code: string
  message: string
  severity: string
  objective_id: string | null
  lesson_id: string | null
  assessment_id: string | null
}

export interface AlignmentReportResponse {
  course_id: string
  draft_id: string
  stage: string
  alignment_report: Record<string, unknown>
}

// Pipeline checkpoint steps
export const COURSE_PIPELINE_STEPS = [
  { number: 0, title: 'Brief', description: 'Topic, audience, Bloom mix' },
  { number: 1, title: 'Objectives', description: 'Measurable learning objectives' },
  { number: 2, title: 'Outline', description: 'Module and lesson sequence' },
  { number: 3, title: 'Lessons', description: 'Lesson content and blocks' },
  { number: 4, title: 'Assessments', description: 'Aligned assessment items' },
  { number: 5, title: 'Publish', description: 'Alignment audit and export' },
] as const

// ── Source Link types (P0-2: Cross-Notebook Source Linking) ──
export interface SourceLinkResponse {
  source_id: string
  notebook_id: string
}

export interface SourceLinkListResponse {
  links: SourceLinkResponse[]
}

export interface SourceLinkCreateRequest {
  source_id: string
  notebook_id: string
}

export interface LinkedSourceInfo {
  id: string
  title: string
  notebook_id: string
  updated: string
}

export interface LinkedSourcesInNotebookResponse {
  sources: LinkedSourceInfo[]
}

// ── Source Context Level types (P1-3) ──
export interface SourceContextLevel {
  source_id: string
  source_title: string
  context_level: string
}

export interface SourceContextLevelList {
  notebook_id: string
  sources: SourceContextLevel[]
}

// ── Notebook Graph types (P0-1: Graph Visualization) ──
export interface GraphNode {
  id: string
  label: string
  type: 'notebook' | 'source' | 'note' | 'insight' | 'chat_session'
  subtitle?: string
  color?: string
}

export interface GraphEdge {
  source: string
  target: string
  label: string
  type: 'reference' | 'artifact' | 'source_link' | 'refers_to' | 'insight_of'
  width?: number
}

export interface GraphResponse {
  notebook_id: string
  notebook_name: string
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// ── Save Chat Message as Note types (P0-3) ──
export interface SaveChatMessageAsNoteRequest {
  message_id: string
  notebook_id: string
  title?: string
}

// ── Notebook Template types (P2-2) ──
export interface NotebookTemplateResponse {
  id: string
  name: string
  description?: string | null
  field?: string | null
  curated?: boolean | null
  expected_completion_duration?: number | null
  default_transformations: string[]
  is_builtin: boolean
}

export interface NotebookTemplateCreate {
  name: string
  description?: string | null
  field?: string | null
  curated?: boolean | null
  expected_completion_duration?: number | null
  default_transformations?: string[]
  is_builtin?: boolean
}

export interface ApplyTemplateRequest {
  template_id: string
  notebook_name?: string | null
}

// ── Notebook Version History types (P2-1) ──
export interface NotebookVersionResponse {
  id: string
  notebook_id: string
  version: number
  name: string
  description?: string | null
  archived?: boolean | null
  field?: string | null
  expected_completion_duration?: number | null
  curated?: boolean | null
  source_count: number
  note_count: number
  created_at: string
  created_by?: string | null
  change_summary?: string | null
  parent_version?: number | null
}

export interface CreateVersionSnapshotRequest {
  notebook_id: string
  change_summary?: string | null
  created_by?: string | null
}

export interface RestoreVersionRequest {
  notebook_id: string
  version: number
}

// ── Search & Ask History types (P1-1: Notebook-Scoped History) ──
export interface SearchHistoryItem {
  id: string
  notebook_id?: string | null
  query: string
  results_count: number
  created?: string | null
}

export interface SearchHistoryListResponse {
  items: SearchHistoryItem[]
  total: number
}

export interface AskHistoryItem {
  id: string
  notebook_id?: string | null
  question: string
  answer_summary?: string | null
  created?: string | null
}

export interface AskHistoryListResponse {
  items: AskHistoryItem[]
  total: number
}
