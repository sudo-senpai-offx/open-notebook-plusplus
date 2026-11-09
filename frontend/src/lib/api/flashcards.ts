import apiClient from './client'

export type FlashcardStatus = 'draft' | 'approved' | 'tombstoned'

export interface Flashcard {
  id: string
  source_id: string | null
  model: 'basic' | 'cloze'
  front: string | null
  back: string | null
  cloze_text: string | null
  tags: string[]
  guid: string | null
  status: FlashcardStatus
}

export interface FlashcardCreate {
  model: 'basic' | 'cloze'
  front?: string
  back?: string
  cloze_text?: string
  tags?: string[]
  source_id?: string
}

export interface FlashcardEdit {
  front?: string
  back?: string
  cloze_text?: string
  tags?: string[]
}

export interface FlashcardGenerateRequest {
  notebook_id: string
  source_id?: string
  source_text: string
  language?: string
  card_type?: 'basic' | 'cloze'
  count?: number
}

export interface FlashcardGenerateResponse {
  ok: boolean
  cards: Array<{ front?: string; back?: string; cloze_text?: string; tags?: string[] }>
  count: number
  error?: string
}

export interface FlashcardExportResponse {
  ok: boolean
  filename: string
  size: number
}

const id = (cardId: string) => encodeURIComponent(cardId)

export const flashcardsApi = {
  list: async (filters?: { status?: FlashcardStatus; sourceId?: string }) => {
    const params = new URLSearchParams()
    if (filters?.status) params.set('status', filters.status)
    if (filters?.sourceId) params.set('source_id', filters.sourceId)
    const query = params.toString()
    const response = await apiClient.get<Flashcard[]>(
      `/flashcards${query ? `?${query}` : ''}`
    )
    return response.data
  },

  approve: async (cardId: string) => {
    const response = await apiClient.post<Flashcard>(`/flashcards/${id(cardId)}/approve`)
    return response.data
  },

  update: async (cardId: string, fields: FlashcardEdit) => {
    const response = await apiClient.put<Flashcard>(`/flashcards/${id(cardId)}`, fields)
    return response.data
  },

  create: async (fields: FlashcardCreate): Promise<Flashcard> => {
    const body: Record<string, unknown> = { model: fields.model }
    for (const key of ['front', 'back', 'cloze_text', 'tags', 'source_id'] as const) {
      const value = fields[key]
      if (value !== undefined) body[key] = value
    }
    const response = await apiClient.post<Flashcard>('/flashcards', body)
    return response.data
  },

  reject: async (cardId: string) => {
    const response = await apiClient.post<{ id: string; deleted: boolean }>(
      `/flashcards/${id(cardId)}/reject`
    )
    return response.data
  },

  tombstone: async (cardId: string) => {
    const response = await apiClient.post<Flashcard>(`/flashcards/${id(cardId)}/tombstone`)
    return response.data
  },

  generate: async (fields: FlashcardGenerateRequest): Promise<FlashcardGenerateResponse> => {
    const response = await apiClient.post<FlashcardGenerateResponse>(
      '/flashcards/generate',
      fields
    )
    return response.data
  },

  export: async (deck?: string): Promise<FlashcardExportResponse> => {
    const params = new URLSearchParams()
    if (deck) params.set('deck', deck)
    const query = params.toString()
    const response = await apiClient.get<FlashcardExportResponse>(
      `/flashcards/export${query ? `?${query}` : ''}`,
      { responseType: 'blob' }
    )
    return response.data
  },
}
