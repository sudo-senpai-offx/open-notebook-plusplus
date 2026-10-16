import apiClient from './client'

export interface ProcessingMemory {
  /** False when /proc/meminfo could not be read. Distinct from available=0:
   * "we could not tell" and "this device has no memory" need different
   * reactions, and only one of them is true here. */
  readable: boolean
  total: number
  available: number
  swapped: number
  /** Bytes held back so the device stays usable while work runs. */
  reserve: number
}

export interface ProcessingStatus {
  /** The promotion window, read fresh by the dispatcher every pass. */
  window: number
  full_throttle: boolean
  /** Jobs in `new` or `running` together - the worker's own semaphore is a
   * backstop, so this is the number that actually governs throughput. */
  in_flight: number
  queue: Record<string, number>
  /** Why held jobs are held. `memory` and `window-full` need different fixes,
   * and `not-yet-considered` means the dispatcher has not looked at them yet. */
  held_reasons: Record<string, number>
  needs_retry: string[]
  memory: ProcessingMemory
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
  /** The promotion window actually stored, after this device's cap. */
  window: number
  /** Always false: the dispatcher reads the window on every pass. */
  restart_required: boolean
  /** True when this device's cap lowered what was asked for. */
  capped: boolean
  /** Upper bound on when the new window takes effect. */
  applied_within_seconds: number
}
