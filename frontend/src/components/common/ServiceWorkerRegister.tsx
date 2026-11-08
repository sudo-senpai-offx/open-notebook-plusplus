"use client"

import { useEffect } from 'react'
import { registerSW } from '@/lib/sw-register'

/** Registers the installability service worker once. No-op without support.
 *
 * Unregisters ALL existing service workers first to eliminate stale cached
 * JS chunks that cause white screen / hydration mismatch after redeploys.
 */
export function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!('serviceWorker' in navigator)) return

    registerSW()
  }, [])
  return null
}
