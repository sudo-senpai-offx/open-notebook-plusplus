import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { SourceStatusBanner } from './SourceStatusBanner'
import { sourcesApi } from '@/lib/api/sources'
import { toast } from 'sonner'

// useTranslation is mocked globally in setup.ts (t returns the key string)

vi.mock('@/lib/api/sources', () => ({
  sourcesApi: {
    retrySource: vi.fn(),
  },
}))

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

const baseSource: any = {
  id: 'source:test-1',
  status: 'ready',
  warnings: [],
  timed_out_pages: [],
}

const renderBanner = (source: any, compact = false) =>
  render(<SourceStatusBanner source={source} compact={compact} />)

describe('SourceStatusBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns null when status is ready with no warnings or timed-out pages', () => {
    const { container } = renderBanner({ ...baseSource, status: 'ready' })
    expect(container.firstChild).toBeNull()
  })

  it('renders an amber badge for extracting_partial', () => {
    renderBanner({ ...baseSource, status: 'extracting_partial' })
    expect(screen.getByText('sources.statusExtractingPartial')).toBeInTheDocument()
  })

  it('renders a destructive badge for extracting_failed', () => {
    renderBanner({ ...baseSource, status: 'extracting_failed' })
    expect(screen.getByText('sources.statusExtractingFailed')).toBeInTheDocument()
  })

  it('renders a blue badge for embedding_pending', () => {
    renderBanner({ ...baseSource, status: 'embedding_pending' })
    expect(screen.getByText('sources.statusEmbeddingPending')).toBeInTheDocument()
  })

  it('formats contiguous timed-out pages as a range', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      timed_out_pages: [6, 7, 8, 9, 10],
    })
    expect(screen.getByText('Pages 6–10 timed out')).toBeInTheDocument()
  })

  it('formats non-contiguous timed-out pages as a list', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      timed_out_pages: [1, 3, 5],
    })
    expect(screen.getByText('Pages 1, 3, 5 timed out')).toBeInTheDocument()
  })

  it('maps javascript_required warning to explanation', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ['javascript_required'],
    })
    expect(
      screen.getByText(
        'This site may require JavaScript. Open the URL in your browser and paste the content as a text source.'
      )
    ).toBeInTheDocument()
  })

  it('maps low_ocr_confidence warning to explanation', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ['low_ocr_confidence'],
    })
    expect(
      screen.getByText(
        'Low OCR confidence detected — some scanned text may contain recognition errors.'
      )
    ).toBeInTheDocument()
  })

  it('maps stt_partial_timeout warning to explanation', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ['stt_partial_timeout'],
    })
    expect(
      screen.getByText('Some audio segments timed out during transcription.')
    ).toBeInTheDocument()
  })

  it('renders raw warning text for unknown warning codes', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ['some_unknown_warning_code'],
    })
    expect(screen.getByText('some_unknown_warning_code')).toBeInTheDocument()
  })

  it('shows STT language selector and reprocess button for Whisper detected warning', () => {
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ["Whisper detected ms (auto-detected)"],
    })
    // Reprocess button uses t('sources.reprocessWithLanguage') → key string
    expect(screen.getByText('sources.reprocessWithLanguage')).toBeInTheDocument()
  })

  it('calls retrySource with language override when reprocessing', async () => {
    vi.mocked(sourcesApi.retrySource).mockResolvedValue({ id: 'source:test-1' } as any)
    renderBanner({
      ...baseSource,
      status: 'extracting_partial',
      warnings: ["Whisper detected ms (auto-detected)"],
    })

    // Reprocess button should be visible (uses t('sources.reprocessWithLanguage'))
    const reprocessBtn = screen.getByText('sources.reprocessWithLanguage')
    expect(reprocessBtn).toBeInTheDocument()

    // Click reprocess button - it defaults to 'auto' language when none selected
    fireEvent.click(reprocessBtn)

    await waitFor(() => {
      expect(sourcesApi.retrySource).toHaveBeenCalledWith('source:test-1', {
        language: 'auto',
      })
    })
  })

  it('shows one-click retry button for extracting_partial', () => {
    renderBanner({ ...baseSource, status: 'extracting_partial' })
    expect(screen.getByText('sources.retryProcessing')).toBeInTheDocument()
  })

  it('shows one-click retry button for extracting_failed', () => {
    renderBanner({ ...baseSource, status: 'extracting_failed' })
    expect(screen.getByText('sources.retryProcessing')).toBeInTheDocument()
  })

  it('shows one-click retry button for embedding_pending', () => {
    renderBanner({ ...baseSource, status: 'embedding_pending' })
    expect(screen.getByText('sources.retryProcessing')).toBeInTheDocument()
  })

  it('does not show retry button for ready status', () => {
    renderBanner({ ...baseSource, status: 'ready', warnings: ['javascript_required'] })
    expect(screen.queryByText('sources.retryProcessing')).not.toBeInTheDocument()
  })

  it('calls retrySource and shows toast on successful retry', async () => {
    vi.mocked(sourcesApi.retrySource).mockResolvedValue({ id: 'source:test-1' } as any)
    renderBanner({ ...baseSource, status: 'extracting_failed' }, false)
    fireEvent.click(screen.getByText('sources.retryProcessing'))

    await waitFor(() => {
      expect(sourcesApi.retrySource).toHaveBeenCalledWith('source:test-1', undefined)
    })
    expect(toast.success).toHaveBeenCalledWith('sources.retryStarted')
  })

  it('disables retry button while in flight', async () => {
    let resolveRetry: (() => void) | null = null
    const retryPromise = new Promise<void>((resolve) => {
      resolveRetry = resolve
    })
    vi.mocked(sourcesApi.retrySource).mockReturnValue(retryPromise as any)

    renderBanner({ ...baseSource, status: 'extracting_partial' })
    fireEvent.click(screen.getByText('sources.retryProcessing'))

    // Button should be disabled while promise is pending
    const button = screen.getByText('sources.retrying')
    expect(button).toBeDisabled()

    // Resolve the promise inside act so React flushes the state update
    act(() => {
      resolveRetry?.()
    })
    await retryPromise

    // Button should be re-enabled
    await waitFor(() => {
      expect(screen.getByText('sources.retryProcessing')).toBeEnabled()
    })
  })

  it('shows error toast when retry fails', async () => {
    vi.mocked(sourcesApi.retrySource).mockRejectedValue(new Error('Network error'))
    renderBanner({ ...baseSource, status: 'extracting_failed' })
    fireEvent.click(screen.getByText('sources.retryProcessing'))

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('sources.retryFailed')
    })
  })

  it('renders compact variant with full-width retry button', () => {
    renderBanner({ ...baseSource, status: 'extracting_partial' }, true)
    const button = screen.getByText('sources.retryProcessing')
    expect(button).toHaveClass('w-full')
  })
})
