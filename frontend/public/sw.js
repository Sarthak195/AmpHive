/**
 * AmpHive service worker: Web Push, plus an app-shell cache for instant open.
 * ==========================================================================
 * Registered on the driver host by src/utils/swControl.js (every visitor) and
 * by Account's push toggle (same URL and scope, so the same registration).
 *
 * Cache Storage holds only public bytes: index.html (the shell) and hashed
 * /assets/*. Personal data never goes here (see src/utils/snapshot.js).
 * /api/ and /socket.io/ are never intercepted. Only a navigation to a driver
 * app route (SHELL_ROUTES) gets the cached shell; OAuth callback and token
 * landing pages, the CPO and admin routes, legal pages, unknown paths (real
 * 404) and root files always go to the network.
 *
 * Update flow: every open revalidates the shell in the background
 * (swapShell), fetching the new index.html and its entry assets together and
 * only then replacing the cached shell, so a deploy reaches a phone on its
 * next open. A shell older than SHELL_MAX_AGE_MS is served network-first.
 *
 * Operator switches (docs/DEPLOYMENT.md, "Service worker and instant open"):
 * - SHELL_CACHE = false: soft off. Caches dropped, no fetch handler, the
 *   worker stays registered (push unaffected).
 * - KILL = true (and SW_KILL in src/utils/swControl.js): caches dropped, no
 *   fetch handler, open pages reload once, and pages unregister the worker
 *   wherever no push subscription exists.
 * - Bump CACHE (and SHELL_CACHE_NAME in swControl.js) to force every open
 *   page off its cached shell at once (a breaking API change).
 * sw.test.js checks the duplicated constants agree.
 *
 * Globals are referenced via `self.*` (the flat ESLint config has no
 * serviceworker environment, and the test stubs one object).
 */

const CACHE = 'amphive-shell-v1';
const SHELL_KEY = '/index.html';
const PRUNE_KEY = '/__amphive-prune';
const SHELL_CACHE = true;
const KILL = false;
const SHELL_ROUTES = /^\/(?:$|(?:session|credit|activity|groups|account|map|wallet|topup|history)(?:\/|$))/;
const SHELL_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const NETWORK_TIMEOUT_MS = 3000;
const SHELL_ON = SHELL_CACHE && !KILL && !self.location.hostname.startsWith('cpo.');

const ASSET_REFS = /\/assets\/[^"'\s>?#]+/g;
const ENTRY_SCRIPT = /<script[^>]*type="module"[^>]*src="\/assets\/[^"]+\.js"/;

// Fetch index.html and its entry assets; replace the cached shell only when
// all of them are cached, so the shell never points at a missing entry script.
async function doSwap() {
  const res = await self.fetch(SHELL_KEY, { cache: 'no-store' });
  const type = res.headers.get('content-type') || '';
  if (res.status !== 200 || res.redirected || !type.includes('text/html')) return;
  const text = await res.text();
  const refs = [...new Set(text.match(ASSET_REFS) || [])];
  // Rejects an interstitial, challenge or maintenance page served as a 200.
  if (refs.length === 0 || !text.includes('<div id="root">') || !ENTRY_SCRIPT.test(text)) return;

  const cache = await self.caches.open(CACHE);
  const old = await cache.match(SHELL_KEY, { ignoreVary: true });
  if (!old || (await old.text()) !== text) {
    // Hashed names never change content, so fetch only the ones not cached.
    const have = await Promise.all(refs.map((p) => cache.match(p, { ignoreVary: true })));
    await cache.addAll(refs.filter((_, i) => !have[i])); // atomic: a failure keeps the old shell
    // Deferred prune: delete what the previous swap marked, then mark the
    // assets the new shell does not reference. The old bundle may still be
    // running in this open and need its lazy chunks.
    const marked = await cache.match(PRUNE_KEY);
    const pending = marked ? await marked.json() : [];
    await Promise.all(pending.filter((p) => !refs.includes(p)).map((p) => cache.delete(p)));
    const next = (await cache.keys())
      .map((r) => new URL(r.url).pathname)
      .filter((p) => p.startsWith('/assets/') && !refs.includes(p));
    await cache.put(PRUNE_KEY, new self.Response(JSON.stringify(next)));
  }
  // Re-put even when unchanged, to restamp the fetch time.
  const headers = new self.Headers(res.headers);
  headers.delete('content-encoding');
  headers.delete('content-length');
  headers.set('x-shell-fetched-at', String(Date.now()));
  await cache.put(SHELL_KEY, new self.Response(text, { status: 200, statusText: 'OK', headers }));
}

let swapping = null;
function swapShell() {
  if (!swapping) {
    swapping = doSwap()
      .catch(() => {})
      .finally(() => {
        swapping = null;
      });
  }
  return swapping;
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      }
    );
  });
}

async function serveShell(request) {
  let cached;
  try {
    cached = await (await self.caches.open(CACHE)).match(SHELL_KEY, { ignoreVary: true });
  } catch {
    cached = undefined;
  }
  if (!cached) return self.fetch(request);
  const age = Date.now() - Number(cached.headers.get('x-shell-fetched-at'));
  if (age >= 0 && age < SHELL_MAX_AGE_MS) return cached;
  // Not opened for a long time: prefer the current app, fall back to the cache.
  try {
    const res = await withTimeout(self.fetch(request), NETWORK_TIMEOUT_MS);
    if (res.ok) return res;
  } catch {
    // Offline or slow: the cached shell below.
  }
  return cached;
}

async function serveAsset(event) {
  const { request } = event;
  let cache;
  try {
    cache = await self.caches.open(CACHE);
    const hit = await cache.match(request, { ignoreVary: true });
    if (hit) return hit;
  } catch {
    cache = undefined;
  }
  const res = await self.fetch(request);
  if (cache && res.status === 200) event.waitUntil(cache.put(request, res.clone()).catch(() => {}));
  return res;
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  // Never reject: a failed install would also break push registration.
  if (SHELL_ON) event.waitUntil(swapShell());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      await self.clients.claim();
      let replaced = false;
      for (const name of await self.caches.keys()) {
        if (!name.startsWith('amphive-shell-') || (name === CACHE && SHELL_ON)) continue;
        await self.caches.delete(name);
        if (name !== CACHE) replaced = true;
      }
      // An older CACHE name was dropped (operator bump): pages running that
      // shell reload. KILL: pages reload once and unregister (swControl.js).
      const type = KILL ? 'sw-killed' : replaced && SHELL_ON ? 'reload' : null;
      if (type) {
        for (const client of await self.clients.matchAll({ type: 'window' })) client.postMessage({ type });
      }
    })().catch(() => {})
  );
});

// Killed, switched off or CPO-host workers register no fetch handler at all:
// every request goes straight to the network, as with the push-only worker.
if (SHELL_ON) {
  self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/socket.io/')) return;
    if (request.mode === 'navigate') {
      if (!SHELL_ROUTES.test(url.pathname)) return;
      event.respondWith(serveShell(request));
      event.waitUntil(swapShell());
      return;
    }
    if (url.pathname.startsWith('/assets/')) event.respondWith(serveAsset(event));
  });
}

// The page asks for a revalidation after load, so the swap does not depend
// only on the navigation event's lifetime (short on iOS).
self.addEventListener('message', (event) => {
  if (SHELL_ON && event.data && event.data.type === 'revalidate') event.waitUntil(swapShell());
});

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
