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

export interface FlashcardEdit {
  front?: string
  back?: string
  cloze_text?: string
  tags?: string[]
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
}
