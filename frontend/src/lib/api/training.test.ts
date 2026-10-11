import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { trainingApi } from './training'

vi.mock('./client', () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
  },
}))

const mockPost = vi.mocked(apiClient.post)

describe('trainingApi.submitTinyTrain', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('submits train_tiny through the generic command endpoint', async () => {
    mockPost.mockResolvedValue({
      data: { job_id: 'job-9', status: 'submitted', message: 'ok' },
    })

    const res = await trainingApi.submitTinyTrain({
      text: 'hello world',
      name: 'smoke',
      maxSteps: 50,
    })

    expect(mockPost).toHaveBeenCalledWith('/commands/jobs', {
      command: 'train_tiny',
      app: 'open_notebook',
      input: { text: 'hello world', name: 'smoke', max_steps: 50 },
    })
    expect(res.job_id).toBe('job-9')
  })

  it('rejects empty text before any request', async () => {
    await expect(
      trainingApi.submitTinyTrain({ text: '   ', name: 'empty' })
    ).rejects.toThrow(/non-empty/i)
    expect(mockPost).not.toHaveBeenCalled()
  })
})
