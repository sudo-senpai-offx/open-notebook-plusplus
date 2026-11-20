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
  AttachmentListResponse,
  AttachmentContentResponse,
  AttachmentDeleteResponse,
  EditMessageRequest,
  EditMessageResponse,
  ProcessingFlagsResponse,
  SessionStatusResponse,
  QuotaStatusResponse,
  AttachmentRecord,
  ChatBuildContextRequest,
  ChatBuildContextResponse,
  ChatEditMessageRequest,
  ChatEditMessageResponse,
  DynamicCourseRequest,
  SyncConflictsResponse,
  SyncOptoutResponse,
  SyncResolveResponse,
  SyncUpdateOptoutResponse,
  SyncAutoSyncResponse,
} from '@/lib/types/api'

export type { AttachmentRecord } from '@/lib/types/api'

export const settingsApi = {
  get: async () => {
    const response = await apiClient.get<SettingsResponse>('/settings')
    return response.data
  },

  update: async (data: Record<string, unknown>) => {
    const response = await apiClient.put<SettingsResponse>('/settings', data)
    return response.data
  },
}

export const sessionApi = {
  getStatus: async () => {
    const response = await apiClient.get<SessionStatusResponse>('/session')
    return response.data
  },
  refresh: async () => {
    const response = await apiClient.post('/session/refresh')
    return response.data
  },
}

export const quotaApi = {
  getStatus: async () => {
    const response = await apiClient.get<QuotaStatusResponse>('/quota')
    return response.data
  },
}

export const deepseekApi = {
  getStatus: async () => {
    const response = await apiClient.get<DeepSeekSessionStatus>('/chat/deepseek-web/session')
    return response.data
  },

  configure: async (data: DeepSeekConfigureRequest) => {
    const response = await apiClient.post<DeepSeekConfigureResponse>(
      '/chat/deepseek-web/session/configure',
      data
    )
    return response.data
  },

  refresh: async () => {
    const response = await apiClient.post<DeepSeekRefreshResponse>(
      '/chat/deepseek-web/session/refresh'
    )
    return response.data
  },

  available: async () => {
    const response = await apiClient.get<DeepSeekAvailableResponse>(
      '/chat/deepseek-web/available'
    )
    return response.data
  },

  chat: async (data: DeepSeekChatRequest) => {
    const response = await apiClient.post<DeepSeekChatResponse>(
      '/chat/deepseek-web',
      data
    )
    return response.data
  },
}

export const processingFlags = {
  get: async () => {
    const response = await apiClient.get<ProcessingFlagsResponse>(
      '/processing-flags'
    )
    return response.data
  },

  update: async (data: {
    allow_local_processing?: boolean
    deepseek_pc_url?: string | null
    google_ai_mode_url?: string | null
    gemini_api_key?: string | null
    duck_ai_url?: string | null
    duck_ai_default_model?: string | null
  }) => {
    const response = await apiClient.put<ProcessingFlagsResponse>(
      '/processing-flags',
      data
    )
    return response.data
  },
}

export const processingFlagsApi = {
  get: async () => {
    const response = await apiClient.get<ProcessingFlagsResponse>(
      '/processing-flags'
    )
    return response.data
  },

  setLimit: async (provider: string, limit: number, reset_at?: string) => {
    const response = await apiClient.put<{ ok: boolean; provider: string; limit: number }>(
      `/services/quota/${provider}`,
      { provider, limit, reset_at }
    )
    return response.data
  },
}

export const attachmentApi = {
  list: async () => {
    const response = await apiClient.get<AttachmentListResponse>('/attachments')
    return response.data
  },

  getContent: async (attachmentId: string) => {
    const response = await apiClient.get<AttachmentContentResponse>(
      `/attachments/${attachmentId}/content`
    )
    return response.data
  },

  delete: async (attachmentId: string) => {
    const response = await apiClient.post<AttachmentDeleteResponse>(
      `/attachments/${attachmentId}/delete`
    )
    return response.data
  },

  rename: async (attachmentId: string, newName: string) => {
    const response = await apiClient.post<AttachmentRecord>(
      `/attachments/${attachmentId}/rename`,
      { name: newName }
    )
    return response.data
  },
}

export const chatApi = {
  buildContext: async (data: ChatBuildContextRequest) => {
    const response = await apiClient.post<ChatBuildContextResponse>(
      '/chat/build-context',
      data
    )
    return response.data
  },

  editMessage: async (sessionId: string, messageId: string, data: ChatEditMessageRequest) => {
    const response = await apiClient.post<ChatEditMessageResponse>(
      `/chat/${sessionId}/messages/${messageId}/edit`,
      data
    )
    return response.data
  },
}

export const dynamicCourseApi = {
  create: async (data: DynamicCourseCreate) => {
    const response = await apiClient.post<DynamicCourseResponse>(
      '/deepseek-sessions/dynamic-course',
      data
    )
    return response.data
  },
}

export const syncApi = {
  getConflicts: async () => {
    const response = await apiClient.get<SyncConflictsResponse>('/sync/conflicts')
    return response.data.conflicts
  },

  getOptout: async () => {
    const response = await apiClient.get<SyncOptoutResponse>('/sync/optout')
    return response.data
  },

  resolveConflict: async (resolution: string, recordId?: string) => {
    const response = await apiClient.post<SyncResolveResponse>(
      `/sync/conflicts/${recordId ?? 'unknown'}/resolve`,
      { resolution }
    )
    return response.data
  },

  updateOptout: async (collections: string[], recordIds: string[]) => {
    const response = await apiClient.post<SyncUpdateOptoutResponse>(
      '/sync/optout',
      { collections, record_ids: recordIds }
    )
    return { collections, record_ids: recordIds }
  },

  autoSync: async () => {
    const response = await apiClient.post<SyncAutoSyncResponse>('/sync/auto-sync')
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

  sttStatus: async () => {
    const response = await apiClient.get<STTResponse>('/tts/status')
    return response.data
  },

  ttsStatus: async () => {
    const response = await apiClient.get<TTSResponse>('/tts/status')
    return response.data
  },

  languages: async () => {
    const response = await apiClient.get<SupportedLanguages>('/tts/languages')
    return response.data
  },
}

export const preprocessorApi = {
  groups: async () => {
    const response = await apiClient.get<PreprocessGroups>(
      '/preprocess/groups'
    )
    return response.data
  },
}

export const browserAiApi = {
  status: async () => {
    const response = await apiClient.get<BrowserAIStatus>(
      '/browser-ai/status'
    )
    return response.data
  },
}

export const flashcardApi = {
  generate: async (data: FlashcardGenerateRequest) => {
    const response = await apiClient.post<FlashcardGenerateResponse>(
      '/flashcards/generate',
      data
    )
    return response.data
  },
}

export const podcastApi = {
  generateScript: async (data: PodcastScriptRequest) => {
    const response = await apiClient.post<PodcastScriptResponse>(
      '/podcasts/script',
      data
    )
    return response.data
  },
}

export const trainingApi = {
  generateSyntheticData: async (data: SyntheticDataRequest) => {
    const response = await apiClient.post<SyntheticDataResponse>(
      '/deepseek-sessions/synthetic-data',
      data
    )
    return response.data
  },
}

export const assessmentApi = {
  create: async (data: AssessmentCreate) => {
    const response = await apiClient.post<AssessmentResponse>(
      '/assessments',
      data
    )
    return response.data
  },

  list: async (params?: { notebook_id?: string; field?: string; limit?: number }) => {
    const searchParams = new URLSearchParams()
    if (params?.notebook_id) searchParams.set('notebook_id', params.notebook_id)
    if (params?.field) searchParams.set('field', params.field)
    if (params?.limit) searchParams.set('limit', String(params.limit))
    const response = await apiClient.get<AssessmentResponse[]>(
      `/assessments?${searchParams.toString()}`
    )
    return response.data
  },

  spiderweb: async () => {
    const response = await apiClient.get<SpiderwebMetricsResponse>(
      '/assessments/spiderweb'
    )
    return response.data
  },
}

export const timetableApi = {
  create: async (data: TimetableBlockCreate) => {
    const response = await apiClient.post<TimetableBlockResponse>(
      '/timetable',
      data
    )
    return response.data
  },

  list: async (params?: { date?: string; notebook_id?: string }) => {
    const searchParams = new URLSearchParams()
    if (params?.date) searchParams.set('date', params.date)
    if (params?.notebook_id) searchParams.set('notebook_id', params.notebook_id)
    const response = await apiClient.get<TimetableBlockResponse[]>(
      `/timetable?${searchParams.toString()}`
    )
    return response.data
  },

  burnoutCheck: async (date: string | number) => {
    const response = await apiClient.get<BurnoutCheckResponse>(
      `/timetable/burnout-check?date=${date}`
    )
    return response.data
  },
}
