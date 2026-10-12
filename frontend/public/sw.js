/* Minimal installability service worker (PWA slice 1).
 * Cache-first app shell with a versioned cache; API calls and navigations
 * to unknown routes always go to network. Offline reading depth (cached
 * sources/review state) is a later slice — this file only earns the
 * install prompt, nothing more. */

const CACHE_VERSION = 'onb-shell-v1'
const SHELL = ['/', '/icons/icon-192.png', '/icons/icon-512.png']

self.addEventListener('install', event => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
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

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) {
    return
  }
  event.respondWith(
    caches.match(event.request).then(hit => hit ?? fetch(event.request))
  )
})
