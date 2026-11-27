'use client'

import { useState, useCallback } from 'react'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Loader2, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import { sourcesApi } from '@/lib/api/sources'
import { toast } from 'sonner'
import { useTranslation } from '@/lib/hooks/use-translation'
import type { SourceListResponse, SourceDetailResponse } from '@/lib/types/api'

/** A source record enriched with observability fields (Week 5 Day 5). */
type ObservableSource = SourceListResponse | SourceDetailResponse & {
  status?: string
  warnings?: string[]
  timed_out_pages?: number[]
}

/** Language options for the STT quick-retry selector. Ordered with the most
 *  common codes first; "auto" lets Whisper re-detect. */
// i18n-todo: language option labels
const STT_LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'auto', label: 'Auto-detect' },
  { value: 'en', label: 'English' },
  { value: 'es', label: 'Español' },
  { value: 'de', label: 'Deutsch' },
  { value: 'fr', label: 'Français' },
  { value: 'ja', label: '日本語' },
  { value: 'zh', label: '中文' },
  { value: 'ko', label: '한국어' },
  { value: 'ms', label: 'Bahasa Melayu' },
  { value: 'ta', label: 'தமிழ்' },
  { value: 'vi', label: 'Tiếng Việt' },
  { value: 'th', label: 'ไทย' },
  { value: 'ar', label: 'العربية' },
  { value: 'ru', label: 'Русский' },
  { value: 'pt', label: 'Português' },
  { value: 'it', label: 'Italiano' },
  { value: 'nl', label: 'Nederlands' },
  { value: 'pl', label: 'Polski' },
  { value: 'tr', label: 'Türkçe' },
  { value: 'hi', label: 'हिन्दी' },
]

/** Map known warning codes to human-readable explanations. Unknown warnings
 *  are rendered as-is. */
// i18n-todo: warning explanation strings
const WARNING_EXPLANATIONS: Record<string, string> = {
  javascript_required:
    'This site may require JavaScript. Open the URL in your browser and paste the content as a text source.',
  low_ocr_confidence:
    'Low OCR confidence detected — some scanned text may contain recognition errors.',
  stt_partial_timeout:
    'Some audio segments timed out during transcription.',
}

/** Format timed-out page indices into human-readable ranges.
 *  [6, 7, 8, 9, 10] → "Pages 6–10"
 *  [1, 3, 5] → "Pages 1, 3, 5"
 */
function formatTimedOutPages(pages: number[]): string {
  if (pages.length === 0) return ''
  const sorted = [...pages].sort((a, b) => a - b)
  const ranges: string[] = []
  let start = sorted[0]
  let end = sorted[0]

  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] === end + 1) {
      end = sorted[i]
    } else {
      ranges.push(start === end ? String(start) : `${start}–${end}`)
      start = sorted[i]
      end = sorted[i]
    }
  }
  ranges.push(start === end ? String(start) : `${start}–${end}`)

  return `Pages ${ranges.join(', ')} timed out`
}

/** Status badge configuration for each non-ready source status. */
// i18n-todo: status badge labels
const STATUS_CONFIG: Record<string, { label: string; variant: 'default' | 'secondary' | 'destructive' | 'outline' }> = {
  extracting_partial: { label: 'Partially Processed', variant: 'default' },
  extracting_failed: { label: 'Extraction Failed', variant: 'destructive' },
  embedding_pending: { label: 'Indexing Pending', variant: 'secondary' },
}

/** Surfaces structured pipeline outcomes (extracting_partial, extracting_failed,
 *  embedding_pending, warnings, timed_out_pages, auto-detected language) so users
 *  never see silent partial extractions or wrong-language transcripts without an
 *  immediate recovery path.
 *
 *  Renders inside the source detail drawer/page (and compact status badges in
 *  source list cards) whenever status !== "ready" or warnings?.length > 0.
 *
 *  All new strings are English-only for now and tagged // i18n-todo so the
 *  post-stabilization translation pass can find them.
 */
export function SourceStatusBanner({
  source,
  compact = false,
  onRetryComplete,
}: {
  source: ObservableSource
  compact?: boolean
  onRetryComplete?: () => void
}) {
  const { t } = useTranslation()
  const [retrying, setRetrying] = useState(false)
  const [retryLanguage, setRetryLanguage] = useState<string | null>(null)

  const status = source.status
  const warnings = source.warnings ?? []
  const timedOutPages = source.timed_out_pages ?? []
  const detectedLanguageWarning = warnings.find((w) =>
    w.toLowerCase().includes('whisper detected')
  )

  const isRetryable =
    status === 'extracting_partial' ||
    status === 'extracting_failed' ||
    status === 'embedding_pending'

  const hasWarnings = warnings.length > 0
  const hasTimedOutPages = timedOutPages.length > 0

  const handleRetry = useCallback(async () => {
    if (!isRetryable) return
    setRetrying(true)
    try {
      const language = retryLanguage || undefined
      await sourcesApi.retrySource(source.id, language ? { language } : undefined)
      toast.success(t('sources.retryStarted'))
      onRetryComplete?.()
    } catch (err) {
      toast.error(t('sources.retryFailed'))
      console.error('Retry failed:', err)
    } finally {
      setRetrying(false)
    }
  }, [source.id, isRetryable, retryLanguage, t, onRetryComplete])

  // No actionable state to surface.
  if (status === 'ready' && !hasWarnings && !hasTimedOutPages) {
    return null
  }

  const statusConfig = STATUS_CONFIG[status ?? '']
  const badgeVariant = statusConfig?.variant ?? 'secondary'
  const badgeLabel = statusConfig?.label ?? status ?? ''

  // i18n-todo: banner strings
  return (
    <Alert
      variant={status === 'extracting_failed' ? 'destructive' : 'default'}
      className={cn(
        'rounded-lg border',
        status === 'extracting_partial' && 'border-amber-500/50 bg-amber-500/5',
        status === 'extracting_failed' && 'border-destructive/50 bg-destructive/5',
        status === 'embedding_pending' && 'border-blue-500/50 bg-blue-500/5'
      )}
    >
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          {/* Status badge */}
          <Badge variant={badgeVariant} className="text-xs font-medium">
            {badgeLabel}
          </Badge>
          {/* Timed-out pages range */}
          {hasTimedOutPages && (
            <span className="text-xs text-muted-foreground">
              {formatTimedOutPages(timedOutPages)}
            </span>
          )}
        </div>

        {/* Warning explanations */}
        {hasWarnings && (
          <div className="flex flex-col gap-1 sm:flex-row sm:items-start">
            {warnings.map((warning, idx) => {
              const explanation = WARNING_EXPLANATIONS[warning] || warning
              return (
                <div key={idx} className="flex items-start gap-1 text-xs text-muted-foreground">
                  <span className="flex-shrink-0">⚠</span>
                  <span>{explanation}</span>
                </div>
              )
            })}
            {/* Detected language quick-select for Whisper detections. */}
            {detectedLanguageWarning && (
              <div className="flex items-center gap-2 mt-2 sm:mt-0">
                <Select
                  value={retryLanguage ?? ''}
                  onValueChange={setRetryLanguage}
                >
                  <SelectTrigger className="w-32">
                    <SelectValue placeholder={t('sources.selectLanguage')} />
                  </SelectTrigger>
                  <SelectContent>
                    {STT_LANGUAGE_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  size="sm"
                  onClick={() => {
                    setRetrying(true)
                    sourcesApi
                      .retrySource(source.id, { language: retryLanguage || 'auto' })
                      .then(() => {
                        toast.success(t('sources.retryStarted'))
                        onRetryComplete?.()
                      })
                      .catch(() => toast.error(t('sources.retryFailed')))
                      .finally(() => setRetrying(false))
                  }}
                  disabled={retrying}
                >
                  {retrying ? (
                    <>Re-processing…</>
                  ) : (
                    <>
                      {t('sources.reprocessWithLanguage')}
                    </>
                  )}
                </Button>
              </div>
            )}
          </div>
        )}

        {/* One-click retry button (full-width on compact, inline otherwise) */}
        {isRetryable && (
          <Button
            size={compact ? 'sm' : 'default'}
            variant="outline"
            onClick={handleRetry}
            disabled={retrying}
            className={compact ? 'w-full sm:w-auto' : ''}
          >
            {retrying ? (
              <>
                <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                {t('sources.retrying')}
              </>
            ) : compact ? (
              <>
                <RefreshCw className="mr-1 h-3 w-3" />
                {t('sources.retryProcessing')}
              </>
            ) : (
              <>
                <RefreshCw className="mr-2 h-3 w-3" />
                {t('sources.retryProcessing')}
              </>
            )}
          </Button>
        )}
      </div>
    </Alert>
  )
}
