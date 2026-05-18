// Service Worker — ABRIU PWA
const CACHE = 'abriu-v2';
const OFFLINE_URLS = ['/static/css/abriu.css'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(OFFLINE_URLS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/static/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  }
});

// ── Push Notifications ────────────────────────────────────────────
self.addEventListener('push', e => {
  let data = { title: 'ABRIU', body: 'Notificação', url: '/dashboard' };
  try { data = JSON.parse(e.data.text()); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      data: { url: data.url },
      vibrate: [200, 100, 200],
      requireInteraction: false,
      tag: 'abriu-notif'
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/dashboard';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes('/dashboard') && 'focus' in c) return c.focus();
      }
      return clients.openWindow(url);
    })
  );
});
