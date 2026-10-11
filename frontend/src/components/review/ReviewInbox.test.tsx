import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ReviewInbox } from './ReviewInbox'
import { flashcardsApi } from '@/lib/api/flashcards'

// t() returns keys (global setup mock)

vi.mock('@/lib/api/flashcards', () => ({
  flashcardsApi: {
    list: vi.fn(),
    approve: vi.fn(),
    update: vi.fn(),
    reject: vi.fn(),
    tombstone: vi.fn(),
  },
}))

const mockList = vi.mocked(flashcardsApi.list)
const mockApprove = vi.mocked(flashcardsApi.approve)

const draftCard = {
  id: 'flashcard:1',
  source_id: null,
  model: 'basic' as const,
  front: 'Q?',
  back: 'A.',
  cloze_text: null,
  tags: [],
  guid: null,
  status: 'draft' as const,
}

describe('ReviewInbox', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList.mockResolvedValue([draftCard])
  })

  it('lists draft cards with lifecycle actions', async () => {
    render(<ReviewInbox />)
    await waitFor(() => expect(mockList).toHaveBeenCalled())
    expect(await screen.findByText('Q? — A.')).toBeDefined()
    expect(screen.getByRole('button', { name: 'review.approve' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'review.reject' })).toBeDefined()
  })

  it('approves and refreshes the list', async () => {
    mockApprove.mockResolvedValue({ ...draftCard, status: 'approved' })
    render(<ReviewInbox />)
    fireEvent.click(await screen.findByRole('button', { name: 'review.approve' }))
    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('flashcard:1'))
    expect(mockList).toHaveBeenCalledTimes(2)
  })

  it('shows the empty state with no cards', async () => {
    mockList.mockResolvedValue([])
    render(<ReviewInbox />)
    expect(await screen.findByText('review.empty')).toBeDefined()
  })
})
