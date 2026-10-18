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

/** One workload class the scheduler can make resident. */
export interface ResidencyRole {
  role: string
  /** A model server answering /health is *not* the same as a resident model -
   * a swapped-out one still answers. This flag is the load-bearing one. */
  resident: boolean
  pid: string
  model: string
  disk_bytes: number
  /** Weight bytes with runtime overhead, i.e. what it costs to keep loaded. */
  resident_bytes: number
  port: number
  /** Lower runs first. Chat is interactive, embedding is bulk. */
  priority: number
  max_concurrent_jobs: number
  /** Seconds this role may sit unused before it is unloaded. */
  idle_seconds: number
  last_used_age: number
}

export interface ResidencyState {
  /** Whether residency control is on for this device at all. */
  enabled: boolean
  /** False when the supervisor could not be reached - distinct from every
   * role being unloaded, which is a real and different state. */
  available: boolean
  roles: Record<string, ResidencyRole> | null
  resident_total_bytes: number | null
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
  residency: ResidencyState
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
