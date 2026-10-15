import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { SessionManager } from './SessionManager'
import { BaseChatSession } from '@/lib/types/api'

vi.mock('@/lib/hooks/use-models', () => ({
  useModels: () => ({ data: [] }),
}))

const base: BaseChatSession = {
  id: 's1',
  title: 'Genomics',
  created: new Date().toISOString(),
  updated: new Date().toISOString(),
  message_count: 4,
}

const props = {
  currentSessionId: null,
  onCreateSession: vi.fn(),
  onSelectSession: vi.fn(),
  onUpdateSession: vi.fn(),
  onDeleteSession: vi.fn(),
  loadingSessions: false,
}

describe('SessionManager sync badge', () => {
  it('marks a session whose transcript is not exported', () => {
    render(
      <SessionManager
        {...props}
        sessions={[{ ...base, transcript_stored: true, transcript_exported: false }]}
      />
    )

    expect(screen.getByText('chat.awaitingSync')).toBeInTheDocument()
  })

  it('shows nothing when the transcript is exported', () => {
    render(
      <SessionManager
        {...props}
        sessions={[{ ...base, transcript_stored: true, transcript_exported: true }]}
      />
    )

    expect(screen.queryByText('chat.awaitingSync')).not.toBeInTheDocument()
  })

  it('shows nothing when sync state is unknown (older backend)', () => {
    render(<SessionManager {...props} sessions={[base]} />)

    expect(screen.queryByText('chat.awaitingSync')).not.toBeInTheDocument()
  })
})
