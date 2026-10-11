import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ReviewQueue } from './ReviewQueue'
import { reviewApi } from '@/lib/api/review'

vi.mock('@/lib/api/review', () => ({
  reviewApi: {
    due: vi.fn(),
    grade: vi.fn(),
    retention: vi.fn(),
  },
}))

const mockDue = vi.mocked(reviewApi.due)
const mockGrade = vi.mocked(reviewApi.grade)

const card = {
  id: 'flashcard:1',
  model: 'basic' as const,
  front: 'Capital of France?',
  back: 'Paris',
  cloze_text: null,
  tags: [],
  guid: 'abc',
}

describe('ReviewQueue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockDue.mockResolvedValue([card])
  })

  it('shows the prompt, reveals, then grades', async () => {
    mockGrade.mockResolvedValue({ id: 'flashcard:1', due: 'later' })
    render(<ReviewQueue />)
    expect(await screen.findByText('Capital of France?')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'review.reveal' }))
    expect(await screen.findByText('Paris')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'review.grade_good' }))
    await waitFor(() => expect(mockGrade).toHaveBeenCalled())
    expect(mockGrade.mock.calls[0][0]).toBe('flashcard:1')
    expect(mockGrade.mock.calls[0][1]).toBe('good')
  })

  it('shows the empty queue state', async () => {
    mockDue.mockResolvedValue([])
    render(<ReviewQueue />)
    expect(await screen.findByText('review.queueEmpty')).toBeDefined()
  })
})
