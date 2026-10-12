import apiClient from './client'

export const commandsApi = {
  /** Jobs waiting for the single worker. Fail-open: unknown means zero. */
  queueDepth: async (): Promise<number> => {
    try {
      const response = await apiClient.get<unknown[]>('/commands/jobs?status_filter=new')
      return Array.isArray(response.data) ? response.data.length : 0
    } catch {
      return 0
    }
  },
}
