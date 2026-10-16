import apiClient from './client'

export interface ProcessingStatus {
  worker_max_tasks: number
  full_throttle: boolean
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
}
