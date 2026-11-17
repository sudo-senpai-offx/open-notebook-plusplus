// API types matching the FastAPI backend response models.

export interface AttachmentRecord {
  id: string
  file_path: string
  filename: string
  mime_type: string
  file_size: number
  upload_session_id?: string | null
  is_orphaned: boolean
  created?: string | null
}

export interface AttachmentListResponse {
  attachments: AttachmentRecord[]
}

export interface AttachmentContentResponse {
  file_path: string
  filename: string
  mime_type: string
  content: string
}

export interface AttachmentDeleteResponse {
  ok: boolean
  message: string
}

export interface EditMessageRequest {
  message_id: string
  new_content: string
  new_attachments?: string[]
}

export interface EditMessageResponse {
  session_id: string
  messages: ChatMessage[]
  edit_count: number
  continuations_deleted: number
}

export interface ChatMessage {
  id: string
  type: 'human' | 'ai'
  content: string
  timestamp?: string
  thinking?: string | null
  edit_count?: number
  original_content?: string | null
  edited_at?: string | null
}

export interface ContextIndicator {
  sources: string[]
  insights: string[]
  notes: string[]
}

export interface SourceChatSession {
  id: string
  title: string
  source_id: string
  model_override?: string | null
  created?: string
  updated?: string
  message_count?: number
  transcript_stored?: boolean
  transcript_exported?: boolean
}

export interface SourceChatSessionWithMessages extends SourceChatSession {
  messages: ChatMessage[]
  context_indicators?: ContextIndicator | null
}

export interface CreateSourceChatSessionRequest {
  title?: string
  model_override?: string | null
  note_ids?: string[]
}

export interface SendMessageRequest {
  message: string
  model_override?: string | null
  history_since?: string | null
  attachments?: string[]
}

export interface ExecuteChatResponse {
  session_id: string
  messages: ChatMessage[]
  history_selection?: {
    kept_exchanges: number
    dropped_exchanges: number
    total_exchanges: number
    reason: string
  }
  cloud_available?: boolean
  switch_to_cloud_suggested?: boolean
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

export interface DeepSeekSessionStatus {
  credentials_configured: boolean
  has_session_id: boolean
  has_authorization_token: boolean
}

export interface Source {
  id: string
  title?: string | null
  topics?: string[] | null
  embedded?: boolean
  embedded_chunks?: number
  insights_count?: number
  origin?: string | null
  status?: string | null
  created?: string
  updated?: string
}

export interface Notebook {
  id: string
  name: string
  description?: string | null
  archived?: boolean
  created?: string
  updated?: string
  source_count?: number
}

export interface SettingsResponse {
  default_content_processing_engine_doc?: string | null
  default_content_processing_engine_url?: string | null
  default_embedding_option?: string | null
  auto_delete_files?: string | null
  docling_ocr?: boolean | null
  docling_formulas?: boolean | null
  docling_vision?: boolean | null
  youtube_preferred_languages?: string[] | null
  allow_local_processing?: boolean | null
  deepseek_ds_session_id?: string | null
  deepseek_authorization_token?: string | null
  deepseek_pc_url?: string | null
}

export interface SettingsUpdate {
  default_content_processing_engine_doc?: string | null
  default_content_processing_engine_url?: string | null
  default_embedding_option?: string | null
  auto_delete_files?: string | null
  docling_ocr?: boolean | null
  docling_formulas?: boolean | null
  docling_vision?: boolean | null
  youtube_preferred_languages?: string[] | null
  allow_local_processing?: boolean | null
  deepseek_ds_session_id?: string | null
  deepseek_authorization_token?: string | null
  deepseek_pc_url?: string | null
}

// --- Site (migrated from Sites page) ---------------------------------------

export interface Site {
  id: string
  url: string
  name?: string | null
  restrict_to_origin: boolean
  follow_sitemap: boolean
  respect_robots_txt: boolean
  max_pages: number
  user_agent?: string | null
  status?: string | null
  last_run_at?: string | null
  last_run_duration?: number | null
  last_error?: string | null
  created: string
  updated: string
  source_count?: number
}

export interface CreateSiteRequest {
  url: string
  name?: string | null
  restrict_to_origin?: boolean | null
  follow_sitemap?: boolean | null
  respect_robots_txt?: boolean | null
  max_pages?: number | null
  user_agent?: string | null
}

export interface UpdateSiteRequest {
  name?: string | null
  restrict_to_origin?: boolean | null
  follow_sitemap?: boolean | null
  respect_robots_txt?: boolean | null
  max_pages?: number | null
  user_agent?: string | null
}

export interface CrawlSiteRequest {
  notebook_ids?: string[]
  transformations?: string[]
  embed?: boolean | null
  max_pages?: number | null
}

export interface CrawlSiteResponse {
  crawl_id: string
  status: string
  message: string
}

export interface SiteSource {
  id: string
  title?: string | null
  url?: string | null
  status?: string | null
  created: string
  updated: string
}