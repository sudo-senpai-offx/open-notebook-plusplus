import apiClient from './client'
import {
  SettingsResponse,
  DeepSeekSessionStatus,
  DeepSeekConfigureRequest,
  DeepSeekConfigureResponse,
  DeepSeekRefreshResponse,
  DeepSeekAvailableResponse,
  DeepSeekChatRequest,
  DeepSeekChatResponse,
  TTSRequest,
  TTSResponse,
  STTResponse,
  VoiceList,
  SupportedLanguages,
  PreprocessRequest,
  PreprocessResponse,
  PreprocessGroups,
  PromptInfo,
  PromptRenderResponse,
  GoogleAIModeRequest,
  GoogleAIModeResponse,
  DuckAIRequest,
  DuckAIResponse,
  BrowserAIStatus,
  FlashcardGenerateRequest,
  FlashcardGenerateResponse,
  PodcastScriptRequest,
  PodcastScriptResponse,
  SyntheticDataRequest,
  SyntheticDataResponse,
  TranscriptPostProcessRequest,
  TranscriptPostProcessResponse,
  AssessmentCreate,
  AssessmentResponse,
  SpiderwebMetricsResponse,
  TimetableBlockCreate,
  TimetableBlockResponse,
  BurnoutCheckResponse,
  DynamicCourseCreate,
  DynamicCourseResponse,
} from '@/lib/types/api'

export const settingsApi = {
  get: async () => {
    const response = await apiClient.get<SettingsResponse>('/settings')
    return response.data
  },

  update: async (data: Partial<SettingsResponse>) => {
    const response = await apiClient.put<SettingsResponse>('/settings', data)
    return response.data
  },
}

export const sessionApi = {
  getStatus: async () => {
    const response = await apiClient.get<SettingsResponse>('/session/status')
    return response.data
  },

  refresh: async () => {
    const response = await apiClient.post<{ session_id: string; last_refresh_at: string; refreshed: boolean }>('/session/refresh')
    return response.data
  },

  getRateLimits: async () => {
    const response = await apiClient.get<{ rate_limits: Record<string, unknown> }>('/session/rate-limits')
    return response.data
  },
}

export const syncApi = {
  getConflicts: async () => {
    const response = await apiClient.get<{ collection: string; db_record_id: string; local_updated?: string; remote_updated?: string; resolved: boolean; resolution?: string }[]>('/sync/conflicts')
    return response.data
  },

  resolveConflict: async (resolution: 'local' | 'remote' | 'merge', merged_data?: Record<string, unknown>) => {
    const response = await apiClient.post<{ ok: boolean; resolution: string; resolved_at: string }>('/sync/resolve', { resolution, merged_data })
    return response.data
  },

  getOptout: async () => {
    const response = await apiClient.get<{ collections: string[]; record_ids: string[] }>('/sync/optout')
    return response.data
  },

  updateOptout: async (collections?: string[], record_ids?: string[]) => {
    const response = await apiClient.put<{ collections: string[]; record_ids: string[] }>('/sync/optout', { collections, record_ids })
    return response.data
  },

  autoSync: async (device?: string) => {
    const response = await apiClient.post<{ ok: boolean; exported: Record<string, unknown>; import_preview: Record<string, unknown>; errors: string[]; dry_run: boolean }>('/sync/auto', { device: device || 'auto' })
    return response.data
  },
}

export const quotaApi = {
  getStatus: async () => {
    const response = await apiClient.get<{
      providers: Record<string, { used: number; limit: number; remaining: number; exceeded: boolean; reset_at?: string; last_used_at?: string }>
      cumulative_used: number
      cumulative_limit: number
      cumulative_remaining: number
      cumulative_exceeded: boolean
      updated_at?: string
    }>('/services/quota')
    return response.data
  },

  setLimit: async (provider: string, limit: number, reset_at?: string) => {
    const response = await apiClient.put<{ ok: boolean; provider: string; limit: number }>(`/services/quota/${provider}`, { provider, limit, reset_at })
    return response.data
  },
}

export const deepseekApi = {
  getStatus: async () => {
    const response = await apiClient.get<DeepSeekSessionStatus>('/chat/deepseek-web/session')
    return response.data
  },

  configure: async (data: DeepSeekConfigureRequest) => {
    const response = await apiClient.post<DeepSeekConfigureResponse>('/chat/deepseek-web/session/configure', data)
    return response.data
  },

  refresh: async () => {
    const response = await apiClient.post<DeepSeekRefreshResponse>('/chat/deepseek-web/session/refresh')
    return response.data
  },

  available: async () => {
    const response = await apiClient.get<DeepSeekAvailableResponse>('/chat/deepseek-web/available')
    return response.data
  },

  chat: async (data: DeepSeekChatRequest) => {
    const response = await apiClient.post<DeepSeekChatResponse>('/chat/deepseek-web', data)
    return response.data
  },
}

export const ttsSttApi = {
  synthesize: async (data: TTSRequest) => {
    const response = await apiClient.post<TTSResponse>('/tts/synthesize', data)
    return response.data
  },

  voices: async () => {
    const response = await apiClient.get<VoiceList>('/tts/voices')
    return response.data
  },

  transcribe: async (file: File, language?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    if (language) formData.append('language', language)
    const response = await apiClient.post<STTResponse>('/stt/transcribe', formData)
    return response.data
  },

  languages: async () => {
    const response = await apiClient.get<SupportedLanguages>('/stt/languages')
    return response.data
  },

  postprocess: async (data: TranscriptPostProcessRequest) => {
    const response = await apiClient.post<TranscriptPostProcessResponse>('/stt/postprocess', data)
    return response.data
  },

  ttsStatus: async () => {
    const response = await apiClient.get<{ allow_local_processing: boolean; piper_reachable: boolean; piper_detail: string }>('/tts/status')
    return response.data
  },

  sttStatus: async () => {
    const response = await apiClient.get<{ allow_local_processing: boolean; whisper_reachable: boolean; whisper_detail: string }>('/stt/status')
    return response.data
  },
}

export const preprocessorApi = {
  preprocess: async (data: PreprocessRequest) => {
    const response = await apiClient.post<PreprocessResponse>('/preprocess', data)
    return response.data
  },

  groups: async () => {
    const response = await apiClient.get<PreprocessGroups>('/preprocess/groups')
    return response.data
  },
}

export const promptLibraryApi = {
  list: async () => {
    const response = await apiClient.get<PromptInfo[]>('/prompts')
    return response.data
  },

  get: async (name: string) => {
    const response = await apiClient.get<PromptInfo>(`/prompts/${encodeURIComponent(name)}`)
    return response.data
  },

  render: async (name: string, variables: Record<string, unknown>) => {
    const response = await apiClient.post<PromptRenderResponse>(`/prompts/${encodeURIComponent(name)}/render`, { variables })
    return response.data
  },
}

export const browserAiApi = {
  googleAiMode: async (data: GoogleAIModeRequest) => {
    const response = await apiClient.post<GoogleAIModeResponse>('/browser-ai/google-ai-mode', data)
    return response.data
  },

  duckAi: async (data: DuckAIRequest) => {
    const response = await apiClient.post<DuckAIResponse>('/browser-ai/duck-ai', data)
    return response.data
  },

  status: async () => {
    const response = await apiClient.get<BrowserAIStatus>('/browser-ai/status')
    return response.data
  },
}

export const flashcardApi = {
  generate: async (data: FlashcardGenerateRequest) => {
    const response = await apiClient.post<FlashcardGenerateResponse>('/flashcards/generate', data)
    return response.data
  },
}

export const podcastApi = {
  generateScript: async (data: PodcastScriptRequest) => {
    const response = await apiClient.post<PodcastScriptResponse>('/podcasts/script', data)
    return response.data
  },
}

export const trainingApi = {
  generateSyntheticData: async (data: SyntheticDataRequest) => {
    const response = await apiClient.post<SyntheticDataResponse>('/deepseek-sessions/synthetic-data', data)
    return response.data
  },
}

export const assessmentApi = {
  create: async (data: AssessmentCreate) => {
    const response = await apiClient.post<AssessmentResponse>('/assessments', data)
    return response.data
  },

  list: async (params?: { notebook_id?: string; field?: string; limit?: number }) => {
    const searchParams = new URLSearchParams()
    if (params?.notebook_id) searchParams.set('notebook_id', params.notebook_id)
    if (params?.field) searchParams.set('field', params.field)
    if (params?.limit) searchParams.set('limit', String(params.limit))
    const response = await apiClient.get<AssessmentResponse[]>(`/assessments?${searchParams.toString()}`)
    return response.data
  },

  spiderweb: async () => {
    const response = await apiClient.get<SpiderwebMetricsResponse>('/assessments/spiderweb')
    return response.data
  },
}

export const timetableApi = {
  create: async (data: TimetableBlockCreate) => {
    const response = await apiClient.post<TimetableBlockResponse>('/timetable', data)
    return response.data
  },

  list: async (params?: { date?: string; notebook_id?: string }) => {
    const searchParams = new URLSearchParams()
    if (params?.date) searchParams.set('date', params.date)
    if (params?.notebook_id) searchParams.set('notebook_id', params.notebook_id)
    const response = await apiClient.get<TimetableBlockResponse[]>(`/timetable?${searchParams.toString()}`)
    return response.data
  },

  burnoutCheck: async (date: string) => {
    const response = await apiClient.get<BurnoutCheckResponse>(`/timetable/burnout-check?date=${date}`)
    return response.data
  },
}

export const dynamicCourseApi = {
  create: async (data: DynamicCourseCreate) => {
    const response = await apiClient.post<DynamicCourseResponse>('/dynamic-course', data)
    return response.data
  },
}