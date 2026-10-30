import apiClient from './client'
import { SettingsResponse } from '@/lib/types/api'

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
  }
}