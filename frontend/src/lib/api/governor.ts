import apiClient from './client'

export interface GovernorState {
  asleep: boolean
}

export const governorApi = {
  /** Ask the backend to restart a hibernated worker. Authed like other calls. */
  wake: async (): Promise<{ woke: boolean }> => {
    const response = await apiClient.post<{ woke: boolean }>('/governor/wake')
    return response.data
  },

  /** Return whether the worker is currently hibernated by the governor. */
  state: async (): Promise<GovernorState> => {
    const response = await apiClient.get<GovernorState>('/governor/state')
    return response.data
  },
}
