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

/** Hold reasons the backend can report, mapped to their translation keys.
 *
 * An explicit map rather than a template literal, for two reasons. The i18n
 * tooling scans source for literal keys and cannot see a dynamic one, so
 * `processing.reason.${reason}` silently produced five "unused" keys. And a
 * reason this build does not know about - a newer backend on the other device
 * - then renders as a raw key instead of falling back to something readable.
 */
const HOLD_REASON_KEYS: Record<string, string> = {
  memory: 'processing.reason_memory',
  'window-full': 'processing.reason_window-full',
  'unknown-footprint': 'processing.reason_unknown-footprint',
  'memory-unreadable': 'processing.reason_memory-unreadable',
  'not-yet-considered': 'processing.reason_not-yet-considered',
}

/** Bytes as a short human string, for memory figures only.
 *
 * Deliberately not the project's general formatter: this panel shows figures
 * that can legitimately be zero because nothing was readable, and a general
 * helper would render that as "0 B" - which reads as a measurement rather
 * than an absence. The caller decides which case it is.
 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

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
  const [throttleMessage, setThrottleMessage] = useState<string | null>(null)

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
      (status?.queue?.new ?? 0) +
      (status?.queue?.running ?? 0) +
      (status?.queue?.held ?? 0) >
      0
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

  const throttleMutation = useMutation({
    mutationFn: (enabled: boolean) => processingApi.setThrottle(enabled),
    onSuccess: (data) => {
      // The response carries the window that was actually stored, which may
      // be lower than the one the button asked for on a capped device.
      setThrottleMessage(
        data.capped
          ? t('processing.throttleCapped', { count: data.window })
          : t('processing.throttleApplied', { count: data.window })
      )
      refresh()
    },
    onError: () => setThrottleMessage(t('apiErrors.failedToQueue')),
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
  // `held` is the queue's waiting room: admitted work the dispatcher has not
  // promoted yet. Shown separately from `new` because a device with 200
  // waiting and nothing running is a very different situation from one with
  // 200 ready to claim, and collapsing them hides exactly the case the
  // memory gate exists to create.
  const held = status?.queue?.held ?? 0

  // Held jobs are only *considered* once the dispatcher runs a pass, so a
  // hold is either a named reason or "not yet looked at". Showing the raw
  // count without a reason is the spinner this panel is supposed to avoid.
  const holdReasons = Object.entries(status?.held_reasons ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([reason, count]) => ({ reason, count }))

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
                {t('processing.workers', { count: status?.window ?? 1 })}
              </Badge>
              <Badge variant="outline">
                {t('processing.pending', { count: pending })}
              </Badge>
              {running > 0 && (
                <Badge variant="outline">
                  {t('processing.running', { count: running })}
                </Badge>
              )}
              {held > 0 && (
                <Badge variant="outline">
                  {t('processing.held', { count: held })}
                </Badge>
              )}
              {failed > 0 && (
                <Badge variant="destructive">
                  {t('processing.failed', { count: failed })}
                </Badge>
              )}
              {pending === 0 && running === 0 && failed === 0 && held === 0 && (
                <span className="text-xs text-muted-foreground">
                  {t('processing.idle')}
                </span>
              )}
            </div>

            {/* Memory headroom. A hold is only diagnosable if the numbers
                behind it are visible: "nothing is running" and "everything
                is waiting on memory" need different fixes, and this is the
                difference. Shown only when there is something to say - an
                idle device should not render a row of healthy figures. */}
            {status?.memory.readable && held > 0 && (
              <p className="text-xs text-muted-foreground">
                {t('processing.memoryLine', {
                  available: formatBytes(status.memory.available),
                  total: formatBytes(status.memory.total),
                  reserve: formatBytes(status.memory.reserve),
                })}
              </p>
            )}
            {!status?.memory.readable && status !== null && (
              <p className="text-xs text-muted-foreground">
                {t('processing.memoryUnknown')}
              </p>
            )}

            {/* One row per hold reason, because each needs a different
                response: memory frees itself, a full window drains, and
                an unknown footprint means the model size could not be
                determined at all. */}
            {holdReasons.length > 0 && (
              <ul className="space-y-1 text-xs text-muted-foreground">
                {holdReasons.map(({ reason, count }) => {
                  const key = HOLD_REASON_KEYS[reason]
                  return (
                    <li key={reason}>
                      {key ? t(key, { count }) : `${reason}: ${count}`}
                    </li>
                  )
                })}
              </ul>
            )}
            {(status?.needs_retry?.length ?? 0) > 0 && (
              <Alert>
                <AlertDescription className="text-xs">
                  {t('processing.needsReview', { count: status?.needs_retry?.length ?? 0 })}
                </AlertDescription>
              </Alert>
            )}
            <div className="flex flex-wrap items-center gap-2">
              {/* `window` is what the dispatcher is actually admitting
                  against, read fresh every pass, so there is no pending
                  state to reconcile and no reason to key off anything
                  else. Toggling is immediate. */}
              <Button
                size="sm"
                variant={(status?.window ?? 1) > 1 ? 'default' : 'outline'}
                onClick={() => throttleMutation.mutate((status?.window ?? 1) <= 1)}
                disabled={throttleMutation.isPending}
              >
                {throttleMutation.isPending ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Gauge className="h-3 w-3" />
                )}
                <span className="ml-1">{t('processing.throttle')}</span>
              </Button>
              {/* A device cap that lowered the request is worth saying out
                  loud: the button shows the mode that was stored, and
                  silently dropping to a different number than the one the
                  toggle implies is the kind of thing that reads as a bug. */}
              {throttleMessage && (
                <span className="text-xs text-muted-foreground">
                  {throttleMessage}
                </span>
              )}
            </div>
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
