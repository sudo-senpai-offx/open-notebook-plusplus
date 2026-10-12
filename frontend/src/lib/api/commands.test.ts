import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { commandsApi } from './commands'

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
  },
}))

const mockGet = vi.mocked(apiClient.get)

describe('commandsApi.queueDepth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('counts new jobs', async () => {
    mockGet.mockResolvedValue({ data: [{}, {}, {}] })
    await expect(commandsApi.queueDepth()).resolves.toBe(3)
    expect(mockGet).toHaveBeenCalledWith('/commands/jobs?status_filter=new')
  })

  it('fails open to zero', async () => {
    mockGet.mockRejectedValue(new Error('down'))
    await expect(commandsApi.queueDepth()).resolves.toBe(0)
  })
})
