/* Minimal installability service worker (PWA slice 1).
 * Offline reading depth (cached sources/review state) is a later slice —
 * this file only earns the install prompt, nothing more.
 *
 * Shape, and why:
 * - Navigations are NETWORK-first. Every route is server-rendered, so the
 *   network is the source of truth and the cache is only an offline
 *   fallback. The previous cache-first handling made every non-`/` route a
 *   guaranteed miss.
 * - Static assets are cache-first, populated on miss.
 * - Every respondWith() path RESOLVES. A rejected promise does not render
 *   a browser error page: Chrome reports ERR_CACHE_MISS and the navigation
 *   dies, so one transient network failure bricks the app. Cached
 *   responses are synthesised as fallbacks instead.
 * - Anything not matched here is left to the browser. Intercepting without
 *   a guaranteed cache hit is pure risk.
 */

const CACHE_VERSION = 'onb-shell-v2'
const SHELL = ['/', '/icons/icon-192.png', '/icons/icon-512.png']
const STATIC_PREFIXES = ['/_next/static/', '/icons/']

self.addEventListener('install', event => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      // Never fail installation over a shell entry: a rejected install
      // leaves the previous worker active, which is worse than no shell.
      .catch(() => self.skipWaiting())
  )
})

self.addEventListener('activate', event => {
  event.waitUntil(
    caches
      .keys()
      .then(keys =>
        Promise.all(keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  )
})

/** Cached shell, or a synthetic response so respondWith always resolves. */
const offlineShell = () =>
  caches
    .match('/')
    .then(hit => hit || new Response('offline', { status: 503 }))

self.addEventListener('fetch', event => {
  const request = event.request
  if (request.method !== 'GET') return

  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) return

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(offlineShell))
    return
  }

  const isStatic = STATIC_PREFIXES.some(prefix => url.pathname.startsWith(prefix))
  if (!isStatic) return

  event.respondWith(
    caches.match(request).then(
      hit =>
        hit ||
        fetch(request)
          .then(response => {
            if (response && response.ok) {
              const copy = response.clone()
              caches.open(CACHE_VERSION).then(cache => cache.put(request, copy))
            }
            return response
          })
          .catch(() => new Response('', { status: 504 }))
    )
  )
})