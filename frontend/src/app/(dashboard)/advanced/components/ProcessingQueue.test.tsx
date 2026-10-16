import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ProcessingQueue } from './ProcessingQueue'

vi.mock('@/lib/api/processing', () => ({
  processingApi: {
    status: vi.fn(),
    ensureInsights: vi.fn(),
    retryFailed: vi.fn(),
    setThrottle: vi.fn(),
  },
}))

import { processingApi } from '@/lib/api/processing'

const mocked = processingApi as unknown as {
  status: ReturnType<typeof vi.fn>
  ensureInsights: ReturnType<typeof vi.fn>
  retryFailed: ReturnType<typeof vi.fn>
  setThrottle: ReturnType<typeof vi.fn>
}

/** A status with the throttle fields the panel reads. Defaults mirror
 * a settled device: strict mode, nothing pending, so each test states
 * only the field it is about. */
function statusWith(overrides: Record<string, unknown> = {}) {
  return {
    worker_max_tasks: 1,
    full_throttle: false,
    throttle_target: 1,
    throttle_restart_pending: false,
    queue: {},
    needs_retry: [],
    ...overrides,
  }
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
    mocked.status.mockResolvedValue(
      statusWith({
        worker_max_tasks: 3,
        full_throttle: true,
        throttle_target: 3,
        queue: { new: 12, running: 2, failed: 1 },
        needs_retry: ['source:x'],
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.workers')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.pending')).toBeInTheDocument()
    expect(screen.getByText('processing.failed')).toBeInTheDocument()
    expect(screen.getByText('processing.needsReview')).toBeInTheDocument()
  })

  it('queues missing insights on demand', async () => {
    mocked.status.mockResolvedValue(statusWith())
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
    mocked.status.mockResolvedValue(statusWith())
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.retryFailed')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.retryFailed').closest('button')).toBeDisabled()
  })

  it('turns full throttle on from strict', async () => {
    mocked.status.mockResolvedValue(statusWith())
    mocked.setThrottle.mockResolvedValue({
      enabled: true,
      worker_max_tasks: 3,
      restart_required: true,
      capped: false,
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttle')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.throttle'))

    await waitFor(() => {
      expect(mocked.setThrottle).toHaveBeenCalledWith(true)
    })
  })

  it('turns full throttle off from throttled', async () => {
    mocked.status.mockResolvedValue(
      statusWith({
        worker_max_tasks: 3,
        full_throttle: true,
        throttle_target: 3,
      })
    )
    mocked.setThrottle.mockResolvedValue({
      enabled: false,
      worker_max_tasks: 1,
      restart_required: true,
      capped: false,
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttle')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.throttle'))

    await waitFor(() => {
      expect(mocked.setThrottle).toHaveBeenCalledWith(false)
    })
  })

  it('keys the toggle off the saved preference while a restart is pending', async () => {
    // The state that matters: full throttle was requested, the worker
    // has not restarted. Keying the button off the *running* value
    // would make a second click re-submit `true` - a toggle that looks
    // dead to the user, who is trying to undo their own click.
    mocked.status.mockResolvedValue(
      statusWith({
        worker_max_tasks: 1,
        full_throttle: false,
        throttle_target: 3,
        throttle_restart_pending: true,
      })
    )
    mocked.setThrottle.mockResolvedValue({
      enabled: false,
      worker_max_tasks: 1,
      restart_required: true,
      capped: false,
    })
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttleRestart')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.throttle'))

    await waitFor(() => {
      expect(mocked.setThrottle).toHaveBeenCalledWith(false)
    })
  })

  it('hides the pending badge when nothing needs restarting', async () => {
    mocked.status.mockResolvedValue(statusWith())
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttle')).toBeInTheDocument()
    })
    expect(screen.queryByText('processing.throttleRestart')).not.toBeInTheDocument()
  })
})
