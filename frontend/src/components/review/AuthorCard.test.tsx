import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AuthorCard } from './AuthorCard'
import { flashcardsApi } from '@/lib/api/flashcards'

vi.mock('@/lib/api/flashcards', () => ({
  flashcardsApi: {
    create: vi.fn(),
  },
}))

const mockCreate = vi.mocked(flashcardsApi.create)

describe('AuthorCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockCreate.mockResolvedValue({ id: 'flashcard:9' } as never)
  })

  it('authors a basic card and clears the form', async () => {
    const onCreated = vi.fn()
    render(<AuthorCard onCreated={onCreated} />)
    fireEvent.change(screen.getByLabelText('review.front'), { target: { value: 'Q?' } })
    fireEvent.change(screen.getByLabelText('review.back'), { target: { value: 'A.' } })
    fireEvent.click(screen.getByRole('button', { name: 'review.authorSave' }))
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith({
      model: 'basic',
      front: 'Q?',
      back: 'A.',
    }))
    expect(onCreated).toHaveBeenCalled()
  })

  it('switches to cloze and authors cloze text', async () => {
    render(<AuthorCard />)
    fireEvent.click(screen.getByRole('button', { name: 'review.model_cloze' }))
    fireEvent.change(screen.getByLabelText('review.clozeText'), {
      target: { value: 'x {{c1::y}}' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'review.authorSave' }))
    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith({
      model: 'cloze',
      cloze_text: 'x {{c1::y}}',
    }))
  })

  it('shows API errors inline', async () => {
    mockCreate.mockRejectedValue(new Error('denied by tier'))
    render(<AuthorCard />)
    fireEvent.change(screen.getByLabelText('review.front'), { target: { value: 'Q?' } })
    fireEvent.click(screen.getByRole('button', { name: 'review.authorSave' }))
    expect(await screen.findByText('denied by tier')).toBeDefined()
  })
})
