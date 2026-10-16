'use client'

import { useState, useEffect, useCallback } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Loader2, RefreshCw, Play, RotateCcw, Gauge } from 'lucide-react'
import { processingApi, type ProcessingStatus } from '@/lib/api/processing'
import { useTranslation } from '@/lib/hooks/use-translation'

/** Device-local processing: queue depth, bulk ensure, failed retry.
 *
 * Everything here acts on this device's queue - nothing crosses the
 * sync channel. Polling runs only while jobs are active (or once on
 * mount); an idle phone pays nothing for this card existing.
 */
export function ProcessingQueue() {
  const { t } = useTranslation()
  const [status, setStatus] = useState<ProcessingStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setError(null)
      setStatus(await processingApi.status())
    } catch {
      setError('unreachable')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  // While work is active, refresh occasionally so counts move without
  // manual reloads. Ten seconds, not one: this card must not become a
  // battery drain on the device it monitors.
  useEffect(() => {
    const active =
      (status?.queue?.new ?? 0) + (status?.queue?.running ?? 0) > 0
    if (!active) return
    const id = setInterval(refresh, 10000)
    return () => clearInterval(id)
  }, [refresh, status?.queue])

  const ensureMutation = useMutation({
    mutationFn: () => processingApi.ensureInsights(),
    onSuccess: (data) => {
      setLastResult(
        t('processing.ensured', {
          queued: data.queued,
          skipped: data.skipped_have_insight + data.skipped_no_text,
        })
      )
      refresh()
    },
    onError: () => setLastResult(t('apiErrors.failedToQueue')),
  })

  const retryMutation = useMutation({
    mutationFn: () => processingApi.retryFailed(),
    onSuccess: (data) => {
      setLastResult(
        t('processing.retried', { count: data.cleared_count })
      )
      refresh()
    },
    onError: () => setLastResult(t('apiErrors.failedToQueue')),
  })

  const pending = status?.queue?.new ?? 0
  const running = status?.queue?.running ?? 0
  const failed = status?.queue?.failed ?? 0

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge className="h-5 w-5" />
          {t('processing.title')}
        </CardTitle>
        <CardDescription>{t('processing.desc')}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : error ? (
          <Alert>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge variant={status?.full_throttle ? 'default' : 'secondary'}>
                {t('processing.workers', { count: status?.worker_max_tasks ?? 1 })}
              </Badge>
              <Badge variant="outline">
                {t('processing.pending', { count: pending })}
              </Badge>
              {running > 0 && (
                <Badge variant="outline">
                  {t('processing.running', { count: running })}
                </Badge>
              )}
              {failed > 0 && (
                <Badge variant="destructive">
                  {t('processing.failed', { count: failed })}
                </Badge>
              )}
              {pending === 0 && running === 0 && failed === 0 && (
                <span className="text-xs text-muted-foreground">
                  {t('processing.idle')}
                </span>
              )}
            </div>
            {(status?.needs_retry?.length ?? 0) > 0 && (
              <Alert>
                <AlertDescription className="text-xs">
                  {t('processing.needsReview', { count: status?.needs_retry?.length ?? 0 })}
                </AlertDescription>
              </Alert>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => ensureMutation.mutate()}
                disabled={ensureMutation.isPending}
              >
                {ensureMutation.isPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Play className="h-3 w-3" />
                )}
                <span className="ml-1">{t('processing.ensureInsights')}</span>
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending || failed === 0}
              >
                {retryMutation.isPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <RotateCcw className="h-3 w-3" />
                )}
                <span className="ml-1">{t('processing.retryFailed')}</span>
              </Button>
              <Button size="sm" variant="ghost" onClick={refresh}>
                <RefreshCw className="h-3 w-3" />
                <span className="ml-1">{t('common.refresh')}</span>
              </Button>
            </div>
            {lastResult && (
              <p className="text-xs text-muted-foreground">{lastResult}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
