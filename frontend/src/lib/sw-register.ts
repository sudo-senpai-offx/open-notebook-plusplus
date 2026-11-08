/**
 * Service Worker registration for ON++ Study OS
 *
 * Unregisters ALL existing service workers first (to eliminate stale
 * cached JS chunks that cause white screen / hydration mismatch), then
 * registers the new minimal SW. Listens for NEW_SW_ACTIVE and reloads
 * the page so the fresh build takes effect immediately.
 */

let registered = false

export function registerSW(): ServiceWorkerRegistration | null {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
    return null
  }

  if (registered) return null

  registered = true

  // Step 1: Unregister ALL existing service workers to clear stale caches
  navigator.serviceWorker.getRegistrations().then((registrations) => {
    registrations.forEach((registration) => {
      registration.unregister().then((success) => {
        if (success) {
          console.log("[sw] Unregistered old service worker:", registration.scope)
        }
      })
    })
  })

  // Step 2: Register the new minimal SW
  navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then(() => {
      // Listen for messages from the SW
      navigator.serviceWorker.addEventListener("message", (event) => {
        if (event.data?.type === "NEW_SW_ACTIVE") {
          // A new SW has activated — reload to pick up fresh build
          window.location.reload()
        }
        if (event.data?.type === "ONLINE") {
          window.dispatchEvent(new CustomEvent("onpp-online"))
        }
      })

      // Request persistent storage
      if ("storage" in navigator && navigator.storage.persist) {
        navigator.storage.persist().then((persisted) => {
          if (persisted) {
            console.log("[sw] Storage marked persistent")
          }
        })
      }
    })
    .catch((error) => {
      console.warn("[sw] Service worker registration failed:", error)
    })

  return null
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
