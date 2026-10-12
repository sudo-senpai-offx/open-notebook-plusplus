import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { metricsApi } from './metrics'

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
  },
}))

const mockGet = vi.mocked(apiClient.get)

describe('metricsApi.summary', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns the summary shape', async () => {
    const summary = {
      due_count: 3,
      reviews_today: 5,
      lapses_7d: 1,
      median_ms: 4000,
      streak_days: 2,
      focus_minutes_7d: 50,
    }
    mockGet.mockResolvedValue({ data: summary })
    await expect(metricsApi.summary()).resolves.toEqual(summary)
    expect(mockGet).toHaveBeenCalledWith('/metrics/summary')
  })
})
