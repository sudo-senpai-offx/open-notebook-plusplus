'use client'

import { useEffect, useState } from 'react'
import { usePathname } from 'next/navigation'
import { Menu } from 'lucide-react'

import { AppSidebar } from './AppSidebar'
import { SetupBanner } from './SetupBanner'
import { Button } from '@/components/ui/button'
import { useSidebarStore } from '@/lib/stores/sidebar-store'
import { useTranslation } from '@/lib/hooks/use-translation'

interface AppShellProps {
  children: React.ReactNode
}

/**
 * Below Tailwind's `lg` the sidebar becomes a slide-over drawer.
 *
 * The desktop build has no responsive layout at all: AppSidebar rendered
 * unconditionally in a flex row, so on a phone viewport it claimed its
 * full w-64 and squeezed `main` to a sliver. That made the app unusable
 * on a phone while leaving the desktop rendering untouched.
 *
 * The breakpoint is mirrored in JS because the drawer needs to know the
 * viewport to render an overlay. `lg` in Tailwind v4 is 64rem = 1024px.
 */
const MOBILE_QUERY = '(max-width: 1023.98px)'

export function AppShell({ children }: AppShellProps) {
  const pathname = usePathname()
  const { t } = useTranslation()
  const [isMobile, setIsMobile] = useState(false)
  // Destructured rather than selector-based, matching AppSidebar: this
  // module's test harness replaces the store with a plain function double.
  const { isMobileOpen, setMobileOpen } = useSidebarStore()

  useEffect(() => {
    const query = window.matchMedia(MOBILE_QUERY)
    const sync = () => setIsMobile(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  // A drawer must not survive the navigation that opened it, nor a
  // rotation that moves us back to the desktop layout.
  useEffect(() => {
    setMobileOpen(false)
  }, [pathname, setMobileOpen])

  useEffect(() => {
    if (!isMobile) setMobileOpen(false)
  }, [isMobile, setMobileOpen])

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Desktop sidebar — the original layout, untouched. */}
      <div className="hidden lg:flex lg:shrink-0">
        <AppSidebar />
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {isMobile && (
          <div className="flex h-14 shrink-0 items-center gap-2 border-b border-sidebar-border bg-sidebar px-3">
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('navigation.openMenu')}
              data-testid="sidebar-open"
              onClick={() => setMobileOpen(true)}
              className="text-sidebar-foreground hover:bg-sidebar-accent"
            >
              <Menu className="h-5 w-5" />
            </Button>
            <span className="font-display text-[15px] font-bold tracking-tight text-sidebar-foreground">
              {t('common.appName')}
            </span>
          </div>
        )}

        <main className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <SetupBanner />
          {children}
        </main>
      </div>

      {isMobile && isMobileOpen && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/60"
            aria-hidden="true"
            onClick={() => setMobileOpen(false)}
          />
          <div className="fixed inset-y-0 left-0 z-50 flex shadow-2xl">
            <AppSidebar alwaysExpanded onNavigate={() => setMobileOpen(false)} />
          </div>
        </>
      )}
    </div>
  )
}