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
  }
}