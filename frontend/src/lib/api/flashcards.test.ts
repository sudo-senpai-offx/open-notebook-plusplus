import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from './client'
import { flashcardsApi } from './flashcards'

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

const mockGet = vi.mocked(apiClient.get)
const mockPost = vi.mocked(apiClient.post)
const mockPut = vi.mocked(apiClient.put)

describe('flashcardsApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lists with status and source filters', async () => {
    mockGet.mockResolvedValue({ data: [] })
    await flashcardsApi.list({ status: 'draft', sourceId: 'source:s1' })
    expect(mockGet).toHaveBeenCalledWith('/flashcards?status=draft&source_id=source%3As1')
  })

  it('approves and tombstones by encoded id', async () => {
    mockPost.mockResolvedValue({ data: { status: 'approved' } })
    await flashcardsApi.approve('flashcard:a:b')
    expect(mockPost).toHaveBeenCalledWith('/flashcards/flashcard%3Aa%3Ab/approve')
    mockPost.mockResolvedValue({ data: { status: 'tombstoned' } })
    await flashcardsApi.tombstone('flashcard:a:b')
    expect(mockPost).toHaveBeenCalledWith('/flashcards/flashcard%3Aa%3Ab/tombstone')
  })

  it('edits draft fields', async () => {
    mockPut.mockResolvedValue({ data: { front: 'Q!' } })
    await flashcardsApi.update('flashcard:x', { front: 'Q!' })
    expect(mockPut).toHaveBeenCalledWith('/flashcards/flashcard%3Ax', { front: 'Q!' })
  })
})
