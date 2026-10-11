'use client'

import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from '@/lib/hooks/use-translation'
import {
  durations,
  isExpired,
  remainingMs,
  useFocusTimerStore,
  type FocusPhase,
  type FocusPreset,
} from '@/lib/stores/focus-timer'

const PRESET_ORDER: FocusPreset[] = ['pomodoro', 'fifty-ten', 'ninety-twenty']

// Static key maps (never constructed dynamically): the unused-key test and
// extraction tooling only see literal t() keys.
const PHASE_KEYS: Record<FocusPhase, string> = {
  idle: 'focus.phase_idle',
  focus: 'focus.phase_focus',
  'short-break': 'focus.phase_short_break',
  'long-break': 'focus.phase_long_break',
}

const PRESET_KEYS: Record<FocusPreset, string> = {
  pomodoro: 'focus.preset_pomodoro',
  'fifty-ten': 'focus.preset_fifty_ten',
  'ninety-twenty': 'focus.preset_ninety_twenty',
}

function formatRemaining(ms: number | null): string {
  if (ms === null) return '--:--'
  const total = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export function FocusTimer() {
  const { t } = useTranslation()
  const {
    preset,
    phase,
    endsAt,
    completedFocus,
    sessionLog,
    setPreset,
    start,
    completePhase,
    cancel,
  } = useFocusTimerStore()
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (phase === 'idle') return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [phase])

  const remaining = remainingMs(endsAt, now)
  const expired = isExpired(endsAt, now)
  const d = durations(preset)

  return (
    <div className="max-w-3xl space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>
            {t('focus.title')} — {t(PHASE_KEYS[phase])}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div data-testid="focus-remaining" className="font-display text-5xl font-bold tabular-nums">
            {formatRemaining(remaining)}
          </div>
          {expired && phase !== 'idle' && (
            <p className="text-sm text-amber-700 dark:text-amber-300">{t('focus.timeUp')}</p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-sm text-muted-foreground" htmlFor="focus-preset">
              {t('focus.preset')}
            </label>
            <select
              id="focus-preset"
              aria-label={t('focus.preset')}
              value={preset}
              disabled={phase !== 'idle'}
              onChange={e => setPreset(e.target.value as FocusPreset)}
              className="rounded border px-2 py-1 text-sm"
            >
              {PRESET_ORDER.map(p => (
                <option key={p} value={p}>
                  {t(PRESET_KEYS[p])} ({durations(p).focus}/{durations(p).shortBreak})
                </option>
              ))}
            </select>
          </div>
          <p className="text-xs text-muted-foreground">
            {t('focus.setProgress', { done: completedFocus % 4, total: 4 })}
          </p>
          <div className="flex flex-wrap gap-2">
            {phase === 'idle' ? (
              <Button onClick={() => start()}>{t('focus.start')}</Button>
            ) : (
              <>
                <Button onClick={() => completePhase()}>{t('focus.complete')}</Button>
                <Button variant="outline" onClick={() => cancel()}>
                  {t('focus.cancel')}
                </Button>
              </>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            {t('focus.currentDurations', {
              focus: d.focus,
              short: d.shortBreak,
              long: d.longBreak,
            })}
          </p>
        </CardContent>
      </Card>
      {sessionLog.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>{t('focus.recentSessions')}</CardTitle>
          </CardHeader>
          <CardContent>
            <ul data-testid="focus-log" className="space-y-1 text-sm">
              {sessionLog.slice(-5).reverse().map((s, i) => (
                <li key={`${s.startedAt}-${i}`}>
                  {t(PHASE_KEYS[s.phase])} ·{' '}
                  {Math.round(s.actualSeconds / 60)} / {Math.round(s.plannedSeconds / 60)} min
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
