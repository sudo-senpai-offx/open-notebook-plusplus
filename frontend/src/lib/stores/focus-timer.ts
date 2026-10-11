import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

/** P5 focus timer: client-authoritative, persisted, recoverable.
 *
 * Ticking never touches the network: the deadline (`endsAt`) is persisted,
 * so reloads, API outages, and governor hibernation all recover remaining
 * time arithmetically. Completed sessions append to a capped local log;
 * server sync (and the hibernate-veto lease) are later slices.
 */

export type FocusPreset = 'pomodoro' | 'fifty-ten' | 'ninety-twenty'
export type FocusPhase = 'idle' | 'focus' | 'short-break' | 'long-break'

export interface PresetDurations {
  focus: number
  shortBreak: number
  longBreak: number
}

export interface CompletedSession {
  startedAt: number
  endedAt: number
  phase: Exclude<FocusPhase, 'idle'>
  plannedSeconds: number
  actualSeconds: number
  preset: FocusPreset
}

const PRESETS: Record<FocusPreset, PresetDurations> = {
  pomodoro: { focus: 25, shortBreak: 5, longBreak: 15 },
  'fifty-ten': { focus: 50, shortBreak: 10, longBreak: 20 },
  'ninety-twenty': { focus: 90, shortBreak: 20, longBreak: 30 },
}

const FOCUS_PER_SET = 4
const MAX_LOG_ENTRIES = 100

export function durations(preset: FocusPreset): PresetDurations {
  return PRESETS[preset]
}

export function nextPhase(phase: FocusPhase, completedFocus: number): FocusPhase {
  if (phase === 'focus') {
    return (completedFocus + 1) % FOCUS_PER_SET === 0 ? 'long-break' : 'short-break'
  }
  if (phase === 'idle') return 'focus'
  return 'idle'
}

export function remainingMs(endsAt: number | null, now: number): number | null {
  if (endsAt === null) return null
  return endsAt - now
}

export function isExpired(endsAt: number | null, now: number): boolean {
  const remaining = remainingMs(endsAt, now)
  return remaining !== null && remaining <= 0
}

interface FocusTimerState {
  preset: FocusPreset
  phase: FocusPhase
  endsAt: number | null
  startedAt: number | null
  completedFocus: number
  sessionLog: CompletedSession[]
  setPreset: (preset: FocusPreset) => void
  start: (now?: number) => void
  completePhase: (now?: number) => void
  cancel: () => void
}

function phaseSeconds(preset: FocusPreset, phase: FocusPhase): number {
  const d = PRESETS[preset]
  if (phase === 'focus') return d.focus * 60
  if (phase === 'short-break') return d.shortBreak * 60
  if (phase === 'long-break') return d.longBreak * 60
  return 0
}

export const useFocusTimerStore = create<FocusTimerState>()(
  persist(
    (set, get) => ({
      preset: 'pomodoro',
      phase: 'idle',
      endsAt: null,
      startedAt: null,
      completedFocus: 0,
      sessionLog: [],

      setPreset: preset => {
        if (get().phase !== 'idle') return
        set({ preset })
      },

      start: (now = Date.now()) => {
        const { preset, phase } = get()
        const running: FocusPhase = phase === 'idle' ? 'focus' : phase
        set({
          phase: running,
          startedAt: now,
          endsAt: now + phaseSeconds(preset, running) * 1000,
        })
      },

      completePhase: (now = Date.now()) => {
        const { preset, phase, startedAt, endsAt, completedFocus, sessionLog } = get()
        if (phase === 'idle' || startedAt === null) return
        const entry: CompletedSession = {
          startedAt,
          endedAt: now,
          phase,
          plannedSeconds: phaseSeconds(preset, phase),
          actualSeconds: Math.max(0, Math.round((now - startedAt) / 1000)),
          preset,
        }
        const log = [...sessionLog, entry].slice(-MAX_LOG_ENTRIES)
        if (phase === 'focus') {
          const done = completedFocus + 1
          const next = done % FOCUS_PER_SET === 0 ? 'long-break' : 'short-break'
          const nextSecs = phaseSeconds(preset, next)
          set({
            phase: next,
            startedAt: now,
            endsAt: now + nextSecs * 1000,
            completedFocus: done,
            sessionLog: log,
          })
          return
        }
        set({
          phase: 'idle',
          startedAt: null,
          endsAt: null,
          completedFocus: phase === 'long-break' ? 0 : completedFocus,
          sessionLog: log,
        })
      },

      cancel: () => {
        set({ phase: 'idle', startedAt: null, endsAt: null })
      },
    }),
    {
      name: 'open-notebook-focus-timer',
      storage: createJSONStorage(() => localStorage),
      partialize: state => ({
        preset: state.preset,
        phase: state.phase,
        endsAt: state.endsAt,
        startedAt: state.startedAt,
        completedFocus: state.completedFocus,
        sessionLog: state.sessionLog,
      }),
    }
  )
)
