import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { governorApi } from './governor'

vi.mock('./client', () => ({
  default: {
    post: vi.fn(),
  },
}))

const mockPost = vi.mocked(apiClient.post)

describe('governorApi.wake', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('posts to the wake endpoint and returns the verdict', async () => {
    mockPost.mockResolvedValue({ data: { woke: true } })
    await expect(governorApi.wake()).resolves.toEqual({ woke: true })
    expect(mockPost).toHaveBeenCalledWith('/governor/wake')
  })
})
