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
    residency: {
      enabled: false,
      available: false,
      roles: null,
      resident_total_bytes: null,
    },
    ...overrides,
  }
}

/** A residency block for a device where the supervisor answered. */
function residencyWith(roles: Record<string, unknown>) {
  return {
    enabled: true,
    available: true,
    roles: {
      chat: {
        role: 'chat',
        resident: false,
        pid: '-',
        model: '-',
        disk_bytes: 1000 * 1024 * 1024,
        resident_bytes: 1100 * 1024 * 1024,
        port: 8080,
        priority: 10,
        max_concurrent_jobs: 1,
        idle_seconds: 300,
        last_used_age: 400,
      },
      embed: {
        role: 'embed',
        resident: true,
        pid: '19094',
        model: 'bge-small-en-v1.5-q8_0.gguf',
        disk_bytes: 35 * 1024 * 1024,
        resident_bytes: 38 * 1024 * 1024,
        port: 8081,
        priority: 50,
        max_concurrent_jobs: 2,
        idle_seconds: 300,
        last_used_age: 2,
      },
      ...roles,
    },
    resident_total_bytes: 38 * 1024 * 1024,
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
describe('residency', () => {
  it('shows nothing at all when the feature is off', async () => {
    mocked.status.mockResolvedValue(statusWith())
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.idle')).toBeInTheDocument()
    })
    expect(screen.queryByText('processing.residency_chat')).not.toBeInTheDocument()
    expect(screen.queryByText('processing.residencyUnreachable')).not.toBeInTheDocument()
  })

  it('shows a loaded role with its model and what it costs', async () => {
    mocked.status.mockResolvedValue(
      statusWith({ residency: residencyWith({}) })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('bge-small-en-v1.5-q8_0.gguf · 38 MB')).toBeInTheDocument()
    })
  })

  it('distinguishes an unloaded model from a loaded one', async () => {
    // The whole reason this section exists. On the phone llama-server read
    // 1222380 kB at launch and 3288 kB ten minutes later - "up" both times, and
    // the second reading was a model paged out to zram. A single up/down row
    // would have shown the same thing for both.
    mocked.status.mockResolvedValue(
      statusWith({ residency: residencyWith({}) })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.residency_unloaded')).toBeInTheDocument()
    })
    expect(screen.getByText('processing.residency_chat')).toBeInTheDocument()
    expect(screen.getByText('processing.residency_embed')).toBeInTheDocument()
  })

  it('says the supervisor did not answer, rather than claiming nothing is loaded', async () => {
    // Both look identical in the UI if you collapse them, and they are
    // opposites: one is a healthy idle phone, the other is a phone whose
    // residency state nobody can see.
    mocked.status.mockResolvedValue(
      statusWith({
        residency: { enabled: true, available: false, roles: null, resident_total_bytes: null },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.residencyUnreachable')).toBeInTheDocument()
    })
    expect(screen.queryByText('processing.residency_chat')).not.toBeInTheDocument()
  })

  it('renders a hold reason it has no translation for', async () => {
    // `residency:chat:<detail>` carries the supervisor's own words, so this
    // case is the normal one rather than an edge case - and showing it beats
    // showing a blank row.
    mocked.status.mockResolvedValue(
      statusWith({
        queue: { held: 1 },
        held_reasons: { 'residency:chat:needs 1258 MB, 900 MB available': 1 },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText(/needs 1258 MB/)).toBeInTheDocument()
    })
  })

  it('translates the two class-limit reasons it does know', async () => {
    mocked.status.mockResolvedValue(
      statusWith({
        queue: { held: 1 },
        held_reasons: { 'class-limit:chat': 1 },
      })
    )
    renderPanel()

    await waitFor(() => {
      expect(screen.getByText('processing.reason_class_limit_chat')).toBeInTheDocument()
    })
  })
})
