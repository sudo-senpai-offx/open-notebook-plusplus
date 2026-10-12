import apiClient from './client'

export interface StudySummary {
  due_count: number
  reviews_today: number
  lapses_7d: number
  median_ms: number | null
  streak_days: number
  focus_minutes_7d: number
}

export const metricsApi = {
  summary: async (): Promise<StudySummary> => {
    const response = await apiClient.get<StudySummary>('/metrics/summary')
    return response.data
  },
}
