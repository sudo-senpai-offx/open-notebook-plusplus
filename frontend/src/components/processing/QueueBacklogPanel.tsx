'use client'

import { useState, useEffect, useCallback } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Loader2, RefreshCw, Play } from 'lucide-react'
import { useToast } from '@/lib/hooks/use-toast'
import { useTranslation } from '@/lib/hooks/use-translation'
import {
  processingApi,
  type QueueDepthResponse,
  type BatchRunNowResponse,
} from '@/lib/api/processing'

/** Format estimated_wait_seconds into a human-readable backlog string. */
function formatBacklogETA(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `~${minutes} min backlog`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes > 0
    ? `~${hours}h ${remainingMinutes}m backlog`
    : `~${hours}h backlog`
}

/** Lane configuration: backend lane value -> display label + i18n key. */
const LANES: Array<{
  value: QueueDepthResponse['lanes'][number]['lane']
  label: string
  key: string
}> = [
  { value: 'instant', label: 'Instant', key: 'queueBacklog.laneInstant' },
  { value: 'heavy', label: 'Heavy (OCR/STT)', key: 'queueBacklog.laneHeavy' },
  { value: 'deferred', label: 'Deferred', key: 'queueBacklog.laneDeferred' },
]

/** Queue backlog panel: per-lane depths, formatted ETA, Ollama residency badge,
 *  "Run Batch Now" action, and visibility-aware polling (pauses when tab is
 *  backgrounded to save Android battery/CPU).
 *
 *  All new strings are English-only for now and tagged // i18n-todo so the
 *  post-stabilization translation pass can find them.
 */
export function QueueBacklogPanel() {
  const { t } = useTranslation()
  const { toast } = useToast()
  const [data, setData] = useState<QueueDepthResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<boolean>(false)
  const [pollTimer, setPollTimer] = useState<ReturnType<
    typeof setInterval
  > | null>(null)

  const refresh = useCallback(async () => {
    try {
      setError(false)
      setData(await processingApi.getQueueDepth())
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  const runBatchNow = useCallback(async () => {
    try {
      const result: BatchRunNowResponse = await processingApi.runBatchNow()
      if (result.accepted) {
        toast({
          title: t('queueBacklog.batchTriggered'),
          description: t('queueBacklog.batchDesc'),
        })
        await refresh()
      }
    } catch {
      toast({
        title: t('queueBacklog.batchFailed'),
        variant: 'destructive',
      })
    }
  }, [refresh, t, toast])

  // Poll every 10s while the tab is visible; pause when backgrounded.
  useEffect(() => {
    if (document.visibilityState !== 'visible') {
      if (pollTimer) {
        clearInterval(pollTimer)
        setPollTimer(null)
      }
      return
    }
    refresh()
    const id = setInterval(refresh, 10000)
    setPollTimer(id)
    return () => clearInterval(id)
  }, [refresh, pollTimer])

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        refresh()
        const id = setInterval(refresh, 10000)
        setPollTimer(id)
      } else if (pollTimer) {
        clearInterval(pollTimer)
        setPollTimer(null)
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      if (pollTimer) clearInterval(pollTimer)
    }
  }, [refresh, pollTimer])

  const lanes = data?.lanes ?? []
  const totalHeld = data?.total_held ?? 0
  const heavyDeferred =
    (lanes.find((l) => l.lane === 'heavy')?.held ?? 0) +
    (lanes.find((l) => l.lane === 'deferred')?.held ?? 0)

  // i18n-todo: queue backlog panel strings
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {t('queueBacklog.title')}
        </CardTitle>
        <CardDescription>{t('queueBacklog.desc')}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : error ? (
          <Alert>
            <AlertDescription>{t('queueBacklog.unavailable')}</AlertDescription>
          </Alert>
        ) : (
          <>
            {/* Per-lane breakdown. */}
            <div className="grid grid-cols-3 gap-2 text-xs">
              {LANES.map((lane) => {
                const row = lanes.find((l) => l.lane === lane.value)
                const held = row?.held ?? 0
                const running = row?.running ?? 0
                const cap = row?.cap ?? 0
                const atCap = row?.at_cap ?? false
                return (
                  <div key={lane.key} className="flex flex-col gap-1">
                    <div className="flex items-center justify-between">
                      <span className="font-medium">{lane.label}</span>
                      <Badge variant={atCap ? 'destructive' : 'outline'}>
                        {held}/{cap}
                      </Badge>
                    </div>
                    {running > 0 && (
                      <span className="text-muted-foreground">
                        {running} running
                      </span>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Backlog ETA + Ollama residency badge. */}
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm">
                {totalHeld > 0 ? (
                  <span className="font-medium">
                    {formatBacklogETA(data?.estimated_wait_seconds ?? 0)}
                  </span>
                ) : (
                  <span className="text-muted-foreground">
                    {t('queueBacklog.idle')}
                  </span>
                )}
              </div>
              <Badge
                variant={
                  data?.ollama_loaded && !data?.chat_idle
                    ? 'default'
                    : data?.ollama_loaded
                      ? 'secondary'
                      : 'outline'
                }
              >
                {data?.ollama_loaded && !data?.chat_idle
                  ? t('queueBacklog.ollamaLoadedWaiting')
                  : data?.ollama_loaded
                    ? t('queueBacklog.ollamaLoadedIdle')
                    : t('queueBacklog.ollamaUnloadedBatch')}
              </Badge>
            </div>

            {/* Run Batch Now action. */}
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={runBatchNow}
                disabled={
                  heavyDeferred === 0 ||
                  !data?.ollama_loaded ||
                  data?.chat_idle
                }
              >
                {heavyDeferred === 0 ? (
                  <span>{t('queueBacklog.batchNothingToRun')}</span>
                ) : !data?.ollama_loaded ? (
                  <span>{t('queueBacklog.batchOllamaUnloaded')}</span>
                ) : data?.chat_idle ? (
                  <span>{t('queueBacklog.batchAlreadyIdle')}</span>
                ) : (
                  <>
                    <Play className="h-3 w-3" />
                    <span className="ml-1">
                      {t('queueBacklog.runBatchNow')}
                    </span>
                  </>
                )}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={refresh}
                disabled={loading}
              >
                <RefreshCw className="h-3 w-3" />
                <span className="ml-1">{t('common.refresh')}</span>
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
