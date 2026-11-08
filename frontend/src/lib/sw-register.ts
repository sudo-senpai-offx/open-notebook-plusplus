/**
 * Service Worker registration for ON++ Study OS
 *
 * Unregisters ALL existing service workers first (to eliminate stale
 * cached JS chunks that cause white screen / hydration mismatch), then
 * registers the new minimal SW. Listens for NEW_SW_ACTIVE and reloads
 * the page so the fresh build takes effect immediately.
 */

export function registerSW(): Promise<ServiceWorkerRegistration | null> {
  if (typeof navigator === "undefined") {
    return Promise.resolve(null)
  }

  try {
    return navigator.serviceWorker.register("/sw.js", { scope: "/" })
      .then((registration) => {
        // Request persistent storage
        if ("storage" in navigator && navigator.storage.persist) {
          navigator.storage.persist().then((persisted) => {
            if (persisted) {
              console.log("[sw] Storage marked persistent")
            }
          })
        }
        return registration
      })
      .catch((error) => {
        console.warn("[sw] Service worker registration failed:", error)
        return null
      })
  } catch {
    return Promise.resolve(null)
  }
}

export function isOnline(): boolean {
  if (typeof navigator === "undefined") return true
  return navigator.onLine
}

export function onOnline(callback: () => void): () => void {
  if (typeof window === "undefined") return () => {}
  window.addEventListener("onpp-online", callback)
  window.addEventListener("online", callback)
  return () => {
    window.removeEventListener("onpp-online", callback)
    window.removeEventListener("online", callback)
  }
}

export function onOffline(callback: () => void): () => void {
  if (typeof window === "undefined") return () => {}
  window.addEventListener("offline", callback)
  return () => {
    window.removeEventListener("offline", callback)
  }
}
