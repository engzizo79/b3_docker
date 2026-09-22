/* B3 Hive — service worker. Push delivery only (no offline caching: this
   app is a live wallet UI, stale cached state is actively dangerous here).

   The payload shape is set by backend/app/notifier.py's _dispatch_push():
   {title, body}. `tag` is the notification's event_type, which lets the OS
   replace a still-visible notification of the same type instead of
   stacking duplicates — a client-side layer on top of the server-side
   smart-coalescing digest. */

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { /* non-JSON payload */ }
  const title = data.title || 'B3 Hive';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/badge-72.png',
    tag: data.tag || title,
    data: { url: data.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil((async () => {
    const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of clientsList) {
      if ('focus' in c) return c.focus();
    }
    return self.clients.openWindow(url);
  })());
});
