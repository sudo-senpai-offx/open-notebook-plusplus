/**
 * Service Worker registration for ON++ Study OS
 *
 * Registers sw.js on page load. On mobile browsers (Termux/Chrome/Android
 * WebView) this enables offline app shell caching. On desktop it does the same.
 */

let registered = false

export function registerSW(): ServiceWorkerRegistration | null {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
    return null
  }

  if (registered) return null

  registered = true

  navigator.serviceWorker
    .register("/sw.js", { scope: "/" })
    .then(() => {
      // Listen for online events from the SW
      navigator.serviceWorker.addEventListener("message", (event) => {
        if (event.data?.type === "ONLINE") {
          // Dispatch a custom event that the app can listen to
          window.dispatchEvent(new CustomEvent("onpp-online"))
        }
      })

      // Request persistent storage so cache isn't evicted under pressure
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
