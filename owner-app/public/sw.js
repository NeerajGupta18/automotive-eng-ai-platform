// CarSaathi service worker: the app shell works offline; the API is never cached (personal data stays on the server).
// Phase D: shows push notifications (reminders, booking updates) and opens the right page when one is tapped.
const CACHE = 'carsaathi-v2'
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()))
})

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname.startsWith('/get-app/')) return
  if (req.mode === 'navigate') {
    // network first so a new deployment shows at once; the cached shell when offline
    e.respondWith(fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put('/', copy)) }
      return res
    }).catch(() => caches.match('/')))
    return
  }
  if (url.pathname.startsWith('/assets/')) {
    // file names contain a content hash: cache first is safe
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)) }
      return res
    })))
  }
})

// A push from the server: the message was encrypted for this browser only (RFC 8291); the browser decrypted it.
self.addEventListener('push', (e) => {
  let m = {}
  try { m = e.data ? e.data.json() : {} } catch { m = { title: 'CarSaathi', body: e.data ? e.data.text() : '' } }
  e.waitUntil(self.registration.showNotification(m.title || 'CarSaathi', {
    body: m.body || '', icon: '/icon-192.png', badge: '/icon-192.png', tag: m.tag || undefined, data: { path: m.path || '/notifications' },
  }))
})

// Tapped: bring an open CarSaathi window to that page, or open one.
self.addEventListener('notificationclick', (e) => {
  e.notification.close()
  const path = (e.notification.data && e.notification.data.path) || '/'
  const url = new URL(path.startsWith('/') ? path : '/', self.location.origin).href
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    const w = wins.find((c) => new URL(c.url).origin === self.location.origin)
    if (w) return w.focus().then(() => w.navigate ? w.navigate(url) : null)
    return self.clients.openWindow(url)
  }))
})
