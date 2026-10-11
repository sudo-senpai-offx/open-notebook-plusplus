import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { reviewApi } from './review'

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

const mockGet = vi.mocked(apiClient.get)
const mockPost = vi.mocked(apiClient.post)

describe('reviewApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists due cards with a limit', async () => {
    mockGet.mockResolvedValue({ data: [] })
    await reviewApi.due(25)
    expect(mockGet).toHaveBeenCalledWith('/review/due?limit=25')
  })

  it('grades with rating and duration', async () => {
    mockPost.mockResolvedValue({ data: { id: 'flashcard:1', due: 'x' } })
    await reviewApi.grade('flashcard:1', 'good', 4200)
    expect(mockPost).toHaveBeenCalledWith('/review/grade/flashcard%3A1', {
      rating: 'good',
      duration_ms: 4200,
    })
  })

  it('reads the retention target', async () => {
    mockGet.mockResolvedValue({ data: { desired_retention: 0.85 } })
    await expect(reviewApi.retention()).resolves.toEqual({ desired_retention: 0.85 })
  })
})
