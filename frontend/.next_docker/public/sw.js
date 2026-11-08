/**
 * ON++ Study OS Service Worker
 *
 * Minimal, safe service worker for offline-first operation.
 * Does NOT precache directory patterns (which fail silently).
 * Uses runtime caching only. Includes cache versioning and
 * automatic reload when a new version is deployed.
 */

const CACHE_NAME = "onpp-shell-v2"

// Install: claim immediately, no precaching (avoids stale chunk issues)
self.addEventListener("install", (event) => {
  self.skipWaiting()
})

// Activate: delete ALL old caches to prevent stale JS chunk serving,
// then claim all clients so the new SW takes control immediately.
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((name) => caches.delete(name)),
      )
    }).then(() => {
      return self.clients.claim()
    }),
  )
})

// Notify all clients that a new SW has activated (page should reload)
self.addEventListener("activate", (event) => {
  event.waitUntil(
    self.clients.matchAll({ includeUncontrolled: true }).then((clients) => {
      clients.forEach((client) => {
        client.postMessage({ type: "NEW_SW_ACTIVE" })
      })
    }),
  )
})

// Fetch: network-first for API, cache-first for static assets (runtime only)
self.addEventListener("fetch", (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Skip non-GET requests and non-http schemes
  if (request.method !== "GET" || !url.protocol.startsWith("http")) {
    return
  }

  // API calls: always go to network (config, models, etc. must be fresh)
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request))
    return
  }

  // Static assets: cache-first with network fallback (runtime caching only)
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        return cached
      }
      return fetch(request).then((response) => {
        // Only cache successful responses for Next.js static assets
        if (
          response.ok &&
          (url.pathname.startsWith("/_next/static/") ||
            url.pathname.startsWith("/static/"))
        ) {
          const responseClone = response.clone()
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseClone)
          })
        }
        return response
      })
    }),
  )
})

// Message handler: notify clients of online status or new SW
self.addEventListener("message", (event) => {
  if (event.data === "sync-online") {
    self.clients.matchAll().then((clients) => {
      clients.forEach((client) => {
        client.postMessage({ type: "ONLINE" })
      })
    })
  }
})
