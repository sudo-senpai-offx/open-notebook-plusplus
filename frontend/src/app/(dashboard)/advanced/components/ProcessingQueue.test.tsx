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

const GIB = 1024 ** 3

/** A status with the fields the panel reads.
 *
 * Defaults mirror a settled device - strict window, nothing queued,
 * readable memory - so each test states only the field it is about. A mock
 * missing a field silently exercises the `?? fallback`, which is how a
 * deleted field keeps looking like it works.
 */
function statusWith(overrides: Record<string, unknown> = {}) {
  return {
    window: 1,
    full_throttle: false,
    in_flight: 0,
    queue: {},
    held_reasons: {},
    needs_retry: [],
    memory: {
      readable: true,
      total: 15 * GIB,
      available: 8 * GIB,
      swapped: 0,
      reserve: 2 * GIB,
    },
    ...overrides,
  }
}

function throttleResult(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true,
    window: 3,
    restart_required: false,
    capped: false,
    applied_within_seconds: 5,
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
        window: 3,
        full_throttle: true,
        in_flight: 3,
        queue: { new: 1, running: 2, held: 12, failed: 1 },
        needs_retry: ['source:x'],
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.workers')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.pending')).toBeInTheDocument()
    expect(screen.getByText('processing.running')).toBeInTheDocument()
    expect(screen.getByText('processing.held')).toBeInTheDocument()
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
    mocked.setThrottle.mockResolvedValue(throttleResult())
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
      statusWith({ window: 3, full_throttle: true, in_flight: 1 })
    )
    mocked.setThrottle.mockResolvedValue(
      throttleResult({ enabled: false, window: 1 })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttle')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.throttle'))

    await waitFor(() => {
      expect(mocked.setThrottle).toHaveBeenCalledWith(false)
    })
  })

  it('confirms the window that was actually stored', async () => {
    // A device cap can store a lower window than the button asked for. The
    // panel reports the stored value, so the number it shows is the number
    // in force - not the one that was requested.
    mocked.status.mockResolvedValue(statusWith())
    mocked.setThrottle.mockResolvedValue(throttleResult({ window: 2, capped: true }))
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.throttle')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('processing.throttle'))

    await waitFor(() => {
      expect(screen.getByText('processing.throttleCapped')).toBeInTheDocument()
    })
  })

  it('shows why jobs are held rather than only how many', async () => {
    // "Nothing is running" and "everything is waiting on memory" need
    // different responses. A bare count hides exactly the case the memory
    // gate exists to create.
    mocked.status.mockResolvedValue(
      statusWith({
        queue: { held: 12, new: 0, running: 0 },
        held_reasons: { memory: 9, 'window-full': 3 },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.reason_memory')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.reason_window-full')).toBeInTheDocument()
    expect(screen.getByText('processing.memoryLine')).toBeInTheDocument()
  })

  it('says so when memory cannot be read', async () => {
    mocked.status.mockResolvedValue(
      statusWith({
        memory: { readable: false, total: 0, available: 0, swapped: 0, reserve: 0 },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.memoryUnknown')).toBeInTheDocument()
    })
    // No figures rendered from a reading that does not exist.
    expect(screen.queryByText('processing.memoryLine')).not.toBeInTheDocument()
  })

  it('renders an unrecognised hold reason as itself', async () => {
    // A newer backend on another device can report a reason this build has
    // never heard of. Showing the raw reason beats showing a blank row.
    mocked.status.mockResolvedValue(
      statusWith({
        queue: { held: 1 },
        held_reasons: { 'some-future-reason': 1 },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText(/some-future-reason/)).toBeInTheDocument()
    })
  })

  it('stays quiet when there is nothing wrong', async () => {
    mocked.status.mockResolvedValue(statusWith())
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.idle')).toBeInTheDocument()
    })
    expect(screen.queryByText('processing.memoryLine')).not.toBeInTheDocument()
    expect(screen.queryByText('processing.held')).not.toBeInTheDocument()
  })
})