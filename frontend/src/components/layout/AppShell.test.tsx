/* eslint-disable @typescript-eslint/no-explicit-any */
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { usePathname } from 'next/navigation'
import { AppShell } from './AppShell'
import { useSidebarStore } from '@/lib/stores/sidebar-store'

vi.mock('./AppSidebar', () => ({
  AppSidebar: ({ alwaysExpanded, onNavigate }: any) => (
    <div
      data-testid="sidebar"
      data-always-expanded={alwaysExpanded ? 'true' : 'false'}
      onClick={onNavigate}
    >
      sidebar
    </div>
  ),
}))

vi.mock('./SetupBanner', () => ({
  SetupBanner: () => <div data-testid="banner">banner</div>,
}))

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
}))

/** Install a matchMedia double reporting a single viewport width. */
const setViewport = (isMobile: boolean) => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes('max-width') ? isMobile : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/**
 * Stand-in for the persisted zustand store. The global test harness
 * replaces it with a plain function, so this reads a mutable object; the
 * component re-reads it on every render, hence the explicit `rerender`
 * after each state change.
 */
const state = { isCollapsed: false, isMobileOpen: false }
const setMobileOpen = (open: boolean) => {
  state.isMobileOpen = open
}
const installStore = () => {
  vi.mocked(useSidebarStore).mockImplementation(
    () =>
      ({
        ...state,
        setMobileOpen,
        toggleCollapse: vi.fn(),
        setCollapsed: vi.fn(),
      }) as any,
  )
}

describe('AppShell responsive layout', () => {
  beforeEach(() => {
    state.isCollapsed = false
    state.isMobileOpen = false
    installStore()
    vi.mocked(usePathname).mockReturnValue('/sources')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('hides the drawer trigger on desktop viewports', () => {
    setViewport(false)
    render(<AppShell>content</AppShell>)

    expect(screen.queryByTestId('sidebar-open')).toBeNull()
    // Desktop sidebar still renders, inside the lg-only container.
    expect(screen.getAllByTestId('sidebar')).toHaveLength(1)
    expect(screen.getByTestId('sidebar').dataset.alwaysExpanded).toBe('false')
  })

  it('shows the drawer trigger on mobile viewports', () => {
    setViewport(true)
    render(<AppShell>content</AppShell>)

    expect(screen.getByTestId('sidebar-open')).toBeDefined()
  })

  it('keeps the drawer closed until the trigger is pressed', () => {
    setViewport(true)
    const { rerender } = render(<AppShell>content</AppShell>)

    expect(screen.getAllByTestId('sidebar')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('sidebar-open'))
    rerender(<AppShell>content</AppShell>)

    // Desktop container plus the overlay drawer.
    expect(screen.getAllByTestId('sidebar')).toHaveLength(2)
    expect(state.isMobileOpen).toBe(true)
  })

  it('renders the drawer always-expanded so labels stay readable', () => {
    setViewport(true)
    // The desktop preference is "collapsed", but a 16px rail as a
    // slide-over would render icons with no labels.
    state.isCollapsed = true
    const { rerender } = render(<AppShell>content</AppShell>)

    fireEvent.click(screen.getByTestId('sidebar-open'))
    rerender(<AppShell>content</AppShell>)

    const drawers = screen.getAllByTestId('sidebar')
    expect(drawers[drawers.length - 1].dataset.alwaysExpanded).toBe('true')
  })

  it('closes the drawer when a nav link is activated', () => {
    setViewport(true)
    const { rerender } = render(<AppShell>content</AppShell>)
    fireEvent.click(screen.getByTestId('sidebar-open'))
    rerender(<AppShell>content</AppShell>)

    const drawers = screen.getAllByTestId('sidebar')
    fireEvent.click(drawers[drawers.length - 1])
    expect(state.isMobileOpen).toBe(false)

    rerender(<AppShell>content</AppShell>)
    expect(screen.getAllByTestId('sidebar')).toHaveLength(1)
  })

  it('closes the drawer on navigation', () => {
    setViewport(true)
    const { rerender } = render(<AppShell>content</AppShell>)
    fireEvent.click(screen.getByTestId('sidebar-open'))
    rerender(<AppShell>content</AppShell>)
    expect(state.isMobileOpen).toBe(true)

    vi.mocked(usePathname).mockReturnValue('/review')
    rerender(<AppShell>content</AppShell>)

    expect(state.isMobileOpen).toBe(false)
  })

  it('always renders its children', () => {
    setViewport(true)
    render(<AppShell>the page</AppShell>)

    expect(screen.getByText('the page')).toBeDefined()
  })
})