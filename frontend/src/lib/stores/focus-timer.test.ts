import { beforeEach, describe, expect, it } from 'vitest'
import {
  durations,
  isExpired,
  nextPhase,
  remainingMs,
  useFocusTimerStore,
} from './focus-timer'

describe('focus timer transitions', () => {
  it('takes a long break every fourth focus session', () => {
    expect(nextPhase('focus', 0)).toBe('short-break')
    expect(nextPhase('focus', 2)).toBe('short-break')
    expect(nextPhase('focus', 3)).toBe('long-break')
    expect(nextPhase('focus', 7)).toBe('long-break')
  })

  it('returns to idle after breaks', () => {
    expect(nextPhase('short-break', 1)).toBe('idle')
    expect(nextPhase('long-break', 4)).toBe('idle')
    expect(nextPhase('idle', 0)).toBe('focus')
  })

  it('ships the researched presets', () => {
    expect(durations('pomodoro')).toEqual({ focus: 25, shortBreak: 5, longBreak: 15 })
    expect(durations('fifty-ten')).toEqual({ focus: 50, shortBreak: 10, longBreak: 20 })
    expect(durations('ninety-twenty')).toEqual({ focus: 90, shortBreak: 20, longBreak: 30 })
  })

  it('computes remaining time from ends_at', () => {
    const now = 1_000_000
    expect(remainingMs(now + 60_000, now)).toBe(60_000)
    expect(remainingMs(null, now)).toBeNull()
    expect(isExpired(now - 1, now)).toBe(true)
    expect(isExpired(now + 1, now)).toBe(false)
  })
})

describe('focus timer store', () => {
  beforeEach(() => {
    useFocusTimerStore.setState({
      preset: 'pomodoro',
      phase: 'idle',
      endsAt: null,
      completedFocus: 0,
      sessionLog: [],
    })
  })

  it('starts a focus session with an ends_at deadline', () => {
    const now = 2_000_000
    useFocusTimerStore.getState().start(now)
    const s = useFocusTimerStore.getState()
    expect(s.phase).toBe('focus')
    expect(s.endsAt).toBe(now + 25 * 60_000)
  })

  it('completing a phase advances and logs the session', () => {
    const store = useFocusTimerStore.getState()
    store.start(0)
    store.completePhase(25 * 60_000)
    const s = useFocusTimerStore.getState()
    expect(s.phase).toBe('short-break')
    expect(s.completedFocus).toBe(1)
    expect(s.sessionLog).toHaveLength(1)
    expect(s.sessionLog[0]).toMatchObject({ phase: 'focus' })
  })

  it('fourth completion takes the long break and resets the set', () => {
    const store = useFocusTimerStore.getState()
    useFocusTimerStore.setState({ ...useFocusTimerStore.getState(), completedFocus: 3 })
    store.start(0)
    store.completePhase(25 * 60_000)
    const s = useFocusTimerStore.getState()
    expect(s.phase).toBe('long-break')
    store.completePhase(15 * 60_000)
    expect(useFocusTimerStore.getState().completedFocus).toBe(0)
  })

  it('cancel drops back to idle without logging', () => {
    const store = useFocusTimerStore.getState()
    store.start(0)
    store.cancel()
    const s = useFocusTimerStore.getState()
    expect(s.phase).toBe('idle')
    expect(s.endsAt).toBeNull()
    expect(s.sessionLog).toHaveLength(0)
  })
})
