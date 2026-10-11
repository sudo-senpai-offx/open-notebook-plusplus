import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, beforeEach } from 'vitest'
import { FocusTimer } from './FocusTimer'
import { useFocusTimerStore } from '@/lib/stores/focus-timer'

// t() returns keys (global setup mock)

function reset() {
  useFocusTimerStore.setState({
    preset: 'pomodoro',
    phase: 'idle',
    endsAt: null,
    startedAt: null,
    completedFocus: 0,
    sessionLog: [],
  })
}

describe('FocusTimer', () => {
  beforeEach(reset)

  it('renders presets and a start control when idle', () => {
    render(<FocusTimer />)
    expect(screen.getByRole('button', { name: 'focus.start' })).toBeDefined()
    expect(screen.getByLabelText('focus.preset')).toBeDefined()
  })

  it('starts a session and shows cancel + complete controls', () => {
    render(<FocusTimer />)
    fireEvent.click(screen.getByRole('button', { name: 'focus.start' }))
    expect(screen.getByRole('button', { name: 'focus.complete' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'focus.cancel' })).toBeDefined()
    expect(useFocusTimerStore.getState().phase).toBe('focus')
  })

  it('shows remaining time while running', () => {
    useFocusTimerStore.setState({
      ...useFocusTimerStore.getState(),
      phase: 'focus',
      startedAt: Date.now(),
      endsAt: Date.now() + 25 * 60_000,
    })
    render(<FocusTimer />)
    expect(screen.getByTestId('focus-remaining')).toBeDefined()
  })

  it('lists completed sessions from the log', () => {
    useFocusTimerStore.setState({
      ...useFocusTimerStore.getState(),
      sessionLog: [
        {
          startedAt: 1,
          endedAt: 2,
          phase: 'focus',
          plannedSeconds: 1500,
          actualSeconds: 1500,
          preset: 'pomodoro',
        },
      ],
    })
    render(<FocusTimer />)
    expect(screen.getByTestId('focus-log')).toBeDefined()
  })
})
