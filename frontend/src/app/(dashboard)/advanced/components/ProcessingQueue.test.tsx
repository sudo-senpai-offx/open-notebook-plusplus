import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ProcessingQueue } from './ProcessingQueue'

vi.mock('@/lib/api/processing', () => ({
  processingApi: {
    status: vi.fn(),
    ensureInsights: vi.fn(),
    retryFailed: vi.fn(),
  },
}))

import { processingApi } from '@/lib/api/processing'

const mocked = processingApi as unknown as {
  status: ReturnType<typeof vi.fn>
  ensureInsights: ReturnType<typeof vi.fn>
  retryFailed: ReturnType<typeof vi.fn>
}

function renderPanel() {
  const client = new QueryClient()
  return render(
    <QueryClientProvider client={client}>
      <ProcessingQueue />
    </QueryClientProvider>
  )
}

describe('ProcessingQueue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows queue depth and worker mode', async () => {
    mocked.status.mockResolvedValue({
      worker_max_tasks: 3,
      full_throttle: true,
      queue: { new: 12, running: 2, failed: 1 },
      needs_retry: ['source:x'],
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.workers')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.pending')).toBeInTheDocument()
    expect(screen.getByText('processing.failed')).toBeInTheDocument()
    expect(screen.getByText('processing.needsReview')).toBeInTheDocument()
  })

  it('queues missing insights on demand', async () => {
    mocked.status.mockResolvedValue({
      worker_max_tasks: 1,
      full_throttle: false,
      queue: {},
      needs_retry: [],
    })
    mocked.ensureInsights.mockResolvedValue({
      queued: 4,
      skipped_have_insight: 10,
      skipped_no_text: 1,
      skipped_unknown: 0,
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.ensureInsights')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.ensureInsights'))

    await waitFor(() => {
      expect(mocked.ensureInsights).toHaveBeenCalledTimes(1)
      expect(screen.getByText('processing.ensured')).toBeInTheDocument()
    })
  })

  it('disables retry when nothing failed', async () => {
    mocked.status.mockResolvedValue({
      worker_max_tasks: 1,
      full_throttle: false,
      queue: {},
      needs_retry: [],
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.retryFailed')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.retryFailed').closest('button')).toBeDisabled()
  })
})
