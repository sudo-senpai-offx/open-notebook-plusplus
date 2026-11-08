/**
 * ON++ Study OS Service Worker
 *
 * Caches the app shell for offline-first loading:
 * - HTML, CSS, JS, images, fonts — Cache API
 * - Dynamic data (notebooks, assessments, etc.) — IndexedDB (via offline-storage.ts)
 *
 * Strategy: Cache-first for app shell assets, network-first for API calls,
 * falling back to IndexedDB cache when offline.
 */

const CACHE_NAME = "onpp-shell-v1"
const ASSETS = [
  "/",
  "/_next/static/css/",
  "/_next/static/js/",
  "/_next/static/media/",
  "/favicon.ico",
  "/manifest.json",
]

// Install: precache app shell
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS.filter(Boolean))
    }),
  )
  self.skipWaiting()
})

// Activate: clean up old caches
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME && name.startsWith("onpp-"))
          .map((name) => caches.delete(name)),
      )
    }),
  )
  self.clients.claim()
})

// Fetch: serve from cache first, fall back to network
self.addEventListener("fetch", (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Skip non-GET requests and chrome-extension / other schemes
  if (request.method !== "GET" || !url.protocol.startsWith("http")) {
    return
  }

  // API calls: network-first, fall back to nothing (offline data handled by IndexedDB in app)
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(request).catch(() => {
        // Return a minimal offline response so the app can handle it gracefully
        return new Response(
          JSON.stringify({ offline: true, message: "You are offline. Cached data is available." }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        )
      }),
    )
    return
  }

  // App shell: cache-first
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) {
        return cached
      }
      return fetch(request).then((response) => {
        // Cache dynamic assets for future offline use
        if (
          response.ok &&
          (url.pathname.startsWith("/_next/") ||
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

// Background sync: when online again, sync pending writes
self.addEventListener("message", (event) => {
  if (event.data === "sync-online") {
    // Notify all clients that we're back online
    self.clients.matchAll().then((clients) => {
      clients.forEach((client) => {
        client.postMessage({ type: "ONLINE" })
      })
    })
  }
})
