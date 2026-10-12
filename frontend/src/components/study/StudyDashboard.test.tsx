import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { StudyDashboard } from './StudyDashboard'
import { metricsApi } from '@/lib/api/metrics'

vi.mock('@/lib/api/metrics', () => ({
  metricsApi: {
    summary: vi.fn(),
  },
}))

const mockSummary = vi.mocked(metricsApi.summary)

const summary = {
  due_count: 7,
  reviews_today: 12,
  lapses_7d: 1,
  median_ms: 4500,
  streak_days: 4,
  focus_minutes_7d: 90,
}

describe('StudyDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockSummary.mockResolvedValue(summary)
  })

  it('renders all six metric cards', async () => {
    render(<StudyDashboard />)
    await waitFor(() => expect(mockSummary).toHaveBeenCalled())
    expect(await screen.findByTestId('study-study.due')).toHaveTextContent('7')
    expect(screen.getByTestId('study-study.streak')).toHaveTextContent('4d')
    expect(screen.getByTestId('study-study.focus')).toHaveTextContent('90m')
    expect(screen.getByTestId('study-study.median')).toHaveTextContent('4.5s')
  })

  it('shows loading state before data arrives', () => {
    mockSummary.mockReturnValue(new Promise(() => {}))
    render(<StudyDashboard />)
    expect(screen.getByText('study.loading')).toBeDefined()
  })
})
