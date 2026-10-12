'use client'

import { useEffect, useRef, useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Brain, Loader2 } from 'lucide-react'
import { useTranslation } from '@/lib/hooks/use-translation'
import { trainingApi } from '@/lib/api/training'

type Phase = 'idle' | 'running' | 'done' | 'failed'

interface TrainResult {
  loss?: number
  steps?: number
  params?: number
  wall_s?: number
}

export default function TrainingPage() {
  const { t } = useTranslation()
  const [name, setName] = useState('tiny-run')
  const [text, setText] = useState('')
  const [maxSteps, setMaxSteps] = useState(200)
  const [phase, setPhase] = useState<Phase>('idle')
  const [result, setResult] = useState<TrainResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ahead, setAhead] = useState<number | null>(null)
  const pollRef = useRef<AbortController | null>(null)

  useEffect(() => {
    return () => {
      pollRef.current?.abort()
    }
  }, [])

  const handleSubmit = async () => {
    pollRef.current?.abort()
    const controller = new AbortController()
    pollRef.current = controller
    setPhase('running')
    setResult(null)
    setError(null)
    setAhead(null)
    try {
      const submitted = await trainingApi.submitTinyTrain({
        text,
        name: name || 'tiny-run',
        maxSteps,
      })
      const { commandsApi } = await import('@/lib/api/commands')
      if (!controller.signal.aborted) setAhead(await commandsApi.queueDepth())
      const status = await trainingApi.waitForCommand(submitted.job_id, {
        intervalMs: 2000,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      if (status?.status === 'completed') {
        setResult((status.result ?? {}) as TrainResult)
        setPhase('done')
      } else {
        setError(status?.error_message || t('training.failed'))
        setPhase('failed')
      }
    } catch (err) {
      if (controller.signal.aborted) return
      setError(err instanceof Error ? err.message : t('training.failed'))
      setPhase('failed')
    }
  }

  const running = phase === 'running'

  return (
    <AppShell>
      <div className="flex-1 overflow-y-auto">
        <div className="p-6 space-y-6">
          <div className="flex items-center gap-4">
            <h1 className="font-display text-2xl font-bold tracking-tight">{t('training.title')}</h1>
          </div>
          <div className="max-w-3xl space-y-6">
            <p className="text-muted-foreground">{t('training.desc')}</p>
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Brain className="h-4 w-4" />
                  {t('training.newRun')}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <Input
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder={t('training.namePlaceholder')}
                  aria-label={t('training.name')}
                />
                <Textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  placeholder={t('training.textPlaceholder')}
                  rows={6}
                  aria-label={t('training.text')}
                />
                <Input
                  type="number"
                  value={maxSteps}
                  min={1}
                  max={2000}
                  onChange={e => setMaxSteps(Number(e.target.value))}
                  aria-label={t('training.maxSteps')}
                />
                <Button onClick={handleSubmit} disabled={running || !text.trim()}>
                  {running && <Loader2 className="h-4 w-4 animate-spin" />}
                  {running ? t('training.running') : t('training.start')}
                </Button>
                {running && ahead !== null && ahead > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {t('training.jobsAhead', { count: ahead })}
                  </p>
                )}
              </CardContent>
            </Card>
            {phase === 'done' && result && (
              <Card>
                <CardHeader>
                  <CardTitle>{t('training.result')}</CardTitle>
                </CardHeader>
                <CardContent>
                  <dl className="grid grid-cols-2 gap-2 text-sm">
                    <dt className="text-muted-foreground">{t('training.loss')}</dt>
                    <dd>{typeof result.loss === 'number' ? result.loss.toFixed(3) : '—'}</dd>
                    <dt className="text-muted-foreground">{t('training.steps')}</dt>
                    <dd>{result.steps ?? '—'}</dd>
                    <dt className="text-muted-foreground">{t('training.params')}</dt>
                    <dd>{result.params ?? '—'}</dd>
                    <dt className="text-muted-foreground">{t('training.wallTime')}</dt>
                    <dd>{result.wall_s ?? '—'}</dd>
                  </dl>
                </CardContent>
              </Card>
            )}
            {phase === 'failed' && error && (
              <Card>
                <CardContent className="pt-6 text-sm text-destructive">{error}</CardContent>
              </Card>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  )
}
