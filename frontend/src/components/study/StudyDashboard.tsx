'use client'

import { useEffect, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from '@/lib/hooks/use-translation'
import { metricsApi, type StudySummary } from '@/lib/api/metrics'

// Static key map (never constructed dynamically): the unused-key test and
// extraction tooling only see literal t() keys.
const CARD_KEYS = [
  'study.due',
  'study.today',
  'study.lapses',
  'study.median',
  'study.streak',
  'study.focus',
] as const

function formatValue(key: (typeof CARD_KEYS)[number], summary: StudySummary): string {
  switch (key) {
    case 'study.due':
      return String(summary.due_count)
    case 'study.today':
      return String(summary.reviews_today)
    case 'study.lapses':
      return String(summary.lapses_7d)
    case 'study.median':
      return summary.median_ms === null ? '—' : `${(summary.median_ms / 1000).toFixed(1)}s`
    case 'study.streak':
      return `${summary.streak_days}d`
    case 'study.focus':
      return `${summary.focus_minutes_7d}m`
  }
}

export function StudyDashboard() {
  const { t } = useTranslation()
  const [summary, setSummary] = useState<StudySummary | null>(null)

  useEffect(() => {
    let cancelled = false
    metricsApi
      .summary()
      .then(s => {
        if (!cancelled) setSummary(s)
      })
      .catch(() => {
        if (!cancelled) setSummary(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (!summary) {
    return <p className="text-sm text-muted-foreground">{t('study.loading')}</p>
  }

  return (
    <div className="grid max-w-4xl grid-cols-2 gap-4 md:grid-cols-3">
      {CARD_KEYS.map(key => (
        <Card key={key}>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground">{t(key)}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="font-display text-3xl font-bold tabular-nums" data-testid={`study-${key}`}>
              {formatValue(key, summary)}
            </p>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}
