import apiClient from './client'

export interface ProcessingStatus {
  worker_max_tasks: number
  full_throttle: boolean
  /** Saved preference disagrees with the live worker: restart to apply. */
  throttle_restart_pending: boolean
  /** Worker count the next restart will use; what the toggle keys off. */
  throttle_target: number
  queue: Record<string, number>
  needs_retry: string[]
}

export interface EnsureInsightsResult {
  queued: number
  skipped_have_insight: number
  skipped_no_text: number
  skipped_unknown: number
}

export interface RetryFailedResult {
  cleared: string[]
  cleared_count: number
}

export const processingApi = {
  status: async (): Promise<ProcessingStatus> => {
    const response = await apiClient.get<ProcessingStatus>('/processing/status')
    return response.data
  },

  ensureInsights: async (limit = 0): Promise<EnsureInsightsResult> => {
    const response = await apiClient.post<EnsureInsightsResult>(
      '/processing/ensure-insights',
      { limit }
    )
    return response.data
  },

  retryFailed: async (): Promise<RetryFailedResult> => {
    const response = await apiClient.post<RetryFailedResult>(
      '/processing/retry-failed',
      {}
    )
    return response.data
  },

  setThrottle: async (enabled: boolean): Promise<ThrottleResult> => {
    const response = await apiClient.post<ThrottleResult>(
      '/processing/throttle',
      { enabled }
    )
    return response.data
  },
}

export interface ThrottleResult {
  enabled: boolean
  worker_max_tasks: number
  restart_required: boolean
  capped: boolean
}
