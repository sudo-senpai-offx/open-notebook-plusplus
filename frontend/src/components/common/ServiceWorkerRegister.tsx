'use client'

import { useEffect } from 'react'

/** Registers the installability service worker once. No-op without support. */
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof window === 'undefined') return
    const sw = 'serviceWorker' in navigator ? navigator.serviceWorker : undefined
    if (!sw) return
    sw.register('/sw.js').catch(() => {
      // Offline shell is best-effort; the app works without it.
    })
  }, [])
  return null
}
