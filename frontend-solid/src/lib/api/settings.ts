import axios, { type AxiosInstance } from 'axios'
import { getLanguage } from '@/lib/i18n'
import type {
  AttachmentListResponse,
  AttachmentContentResponse,
  AttachmentDeleteResponse,
  EditMessageRequest,
  EditMessageResponse,
  BrowserAIStatus,
  DeepSeekSessionStatus,
  SourceChatSession,
  SourceChatSessionWithMessages,
  CreateSourceChatSessionRequest,
  SendMessageRequest,
  Source,
  Notebook,
} from '@/lib/types/api'

// Auth model: the password itself is the Bearer token. /api/auth/status tells
// us whether auth is required; on 401 we clear the stored token and bounce to
// /login.
const TOKEN_KEY = 'onb_token'

const client: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 600000,
  headers: {
    'Content-Type': 'application/json',
  },
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  config.headers['Accept-Language'] = getLanguage()
  if (config.data instanceof FormData) {
    delete config.headers['Content-Type']
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)

export const authApi = {
  status: () =>
    client.get<{ auth_enabled: boolean; message: string }>('/auth/status').then(r => r.data),

  login: (password: string) => {
    localStorage.setItem(TOKEN_KEY, password)
    return Promise.resolve()
  },

  logout: () => {
    localStorage.removeItem(TOKEN_KEY)
  },

  isAuthenticated: () => !!localStorage.getItem(TOKEN_KEY),
}

// --- Notebooks -------------------------------------------------------------

export const notebookApi = {
  list: () => client.get<Notebook[]>('/notebooks').then(r => r.data),

  create: (name: string) =>
    client.post<Notebook>('/notebooks', { name }).then(r => r.data),

  delete: (id: string) =>
    client.delete(`/notebooks/${id}`).then(r => r.data),
}

// --- Sources ----------------------------------------------------------------

export const sourceApi = {
  list: () => client.get<Source[]>('/sources').then(r => r.data),

  get: (id: string) => client.get<Source>(`/sources/${id}`).then(r => r.data),
}

// --- Source chat ------------------------------------------------------------

export const sourceChatApi = {
  listSessions: (sourceId: string) =>
    client.get<SourceChatSession[]>(`/sources/${sourceId}/chat/sessions`).then(r => r.data),

  createSession: (sourceId: string, data: CreateSourceChatSessionRequest) =>
    client.post<SourceChatSession>(
      `/sources/${sourceId}/chat/sessions`,
      data,
    ).then(r => r.data),

  getSession: (sourceId: string, sessionId: string) =>
    client.get<SourceChatSessionWithMessages>(
      `/sources/${sourceId}/chat/sessions/${sessionId}`,
    ).then(r => r.data),

  deleteSession: (sourceId: string, sessionId: string) =>
    client.delete(`/sources/${sourceId}/chat/sessions/${sessionId}`).then(r => r.data),

  sendMessage: (sourceId: string, sessionId: string, data: SendMessageRequest) =>
    client.post<SourceChatSessionWithMessages>(
      `/sources/${sourceId}/chat/sessions/${sessionId}/messages`,
      data,
    ).then(r => r.data),
}

// --- Chat (notebook-level) --------------------------------------------------

export const chatApi = {
  listSessions: (notebookId: string) =>
    client.get('/chat/sessions', { params: { notebook_id: notebookId } }).then(r => r.data),

  createSession: (data: { notebook_id?: string; title?: string }) =>
    client.post('/chat/sessions', data).then(r => r.data),

  deleteSession: (sessionId: string) =>
    client.delete(`/chat/sessions/${sessionId}`).then(r => r.data),

  execute: (sessionId: string, payload: Record<string, unknown>) =>
    client.post(`/chat/execute`, { session_id: sessionId, ...payload }).then(r => r.data),

  editMessage: (sessionId: string, messageId: string, data: EditMessageRequest) =>
    client.post<EditMessageResponse>(
      `/chat/sessions/${sessionId}/messages/${messageId}/edit`,
      data,
    ).then(r => r.data),
}

// --- Attachments ------------------------------------------------------------

export const attachmentApi = {
  list: () =>
    client.get<AttachmentListResponse>('/chat/attachments').then(r => r.data),

  getContent: (attachmentId: string) =>
    client.get<AttachmentContentResponse>(
      `/chat/attachments/${attachmentId}/content`,
    ).then(r => r.data),

  delete: (attachmentId: string) =>
    client.delete<AttachmentDeleteResponse>(
      `/chat/attachments/${attachmentId}`,
    ).then(r => r.data),

  rename: (attachmentId: string, newFilename: string) =>
    client.patch<AttachmentDeleteResponse>(
      `/chat/attachments/${attachmentId}/rename`,
      { new_filename: newFilename },
    ).then(r => r.data),
}

// --- Browser AI / DeepSeek --------------------------------------------------

export const browserAiApi = {
  status: () => client.get<BrowserAIStatus>('/browser-ai/status').then(r => r.data),
}

export const deepseekApi = {
  status: () =>
    client.get<DeepSeekSessionStatus>('/chat/deepseek-web/session').then(r => r.data),
}

export const settingsApi = {
  get: () => client.get('/settings').then(r => r.data),
}