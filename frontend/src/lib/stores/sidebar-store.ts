import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface SidebarState {
  /** Desktop-only: rail (w-16) vs full (w-64). Persisted as a preference. */
  isCollapsed: boolean
  toggleCollapse: () => void
  setCollapsed: (collapsed: boolean) => void
  /**
   * Viewport-only: slide-over drawer state below `lg`. Deliberately NOT
   * persisted — a drawer left open in localStorage would cover the app on
   * the next phone load, long after the viewport that justified it.
   */
  isMobileOpen: boolean
  setMobileOpen: (open: boolean) => void
}

export const useSidebarStore = create<SidebarState>()(
  persist(
    (set) => ({
      isCollapsed: false,
      toggleCollapse: () => set((state) => ({ isCollapsed: !state.isCollapsed })),
      setCollapsed: (collapsed) => set({ isCollapsed: collapsed }),

      isMobileOpen: false,
      setMobileOpen: (open) => set({ isMobileOpen: open }),
    }),
    {
      name: 'sidebar-storage',
      // Only the desktop preference survives a reload.
      partialize: (state) => ({ isCollapsed: state.isCollapsed }),
    }
  )
)