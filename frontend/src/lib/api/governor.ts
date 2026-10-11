import apiClient from './client'

export const governorApi = {
  /** Ask the backend to restart a hibernated worker. Authed like other calls. */
  wake: async (): Promise<{ woke: boolean }> => {
    const response = await apiClient.post<{ woke: boolean }>('/governor/wake')
    return response.data
  },
}
