import apiClient from './client'

export type ReviewRating = 'again' | 'hard' | 'good' | 'easy'

export interface DueCard {
  id: string
  model: 'basic' | 'cloze'
  front: string | null
  back: string | null
  cloze_text: string | null
  tags: string[]
  guid: string | null
}

export interface GradeResult {
  id: string
  due: string
}

export const reviewApi = {
  due: async (limit = 50): Promise<DueCard[]> => {
    const response = await apiClient.get<DueCard[]>(`/review/due?limit=${limit}`)
    return response.data
  },

  grade: async (cardId: string, rating: ReviewRating, durationMs = 0): Promise<GradeResult> => {
    const response = await apiClient.post<GradeResult>(
      `/review/grade/${encodeURIComponent(cardId)}`,
      { rating, duration_ms: durationMs }
    )
    return response.data
  },

  retention: async (): Promise<{ desired_retention: number }> => {
    const response = await apiClient.get<{ desired_retention: number }>('/review/retention')
    return response.data
  },
}
