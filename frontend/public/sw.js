/**
 * AmpHive service worker — Web Push only.
 * Registered by NotificationBell when the user enables push. No fetch
 * handler on purpose: this must never intercept or cache app traffic.
 * (Globals are referenced via `self.*` — the flat ESLint config has no
 * serviceworker environment.)
 */

self.addEventListener('push', (event) => {
  let data;
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'AmpHive';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || '',
      icon: '/favicon.svg',
      badge: '/favicon.svg',
      // Same tag = the OS replaces rather than stacks (e.g. repeated
      // low-balance pushes for one session).
      tag: data.id != null ? `amphive-${data.type}-${data.session_id ?? data.id}` : undefined,
      data: { url: data.session_id != null ? '/session' : '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
