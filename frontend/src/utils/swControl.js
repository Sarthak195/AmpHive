/**
 * Page side of the service worker (public/sw.js): registration, the kill
 * switch, and recovery when an old shell asks for a chunk the server no
 * longer has. See docs/DEPLOYMENT.md, "Service worker and instant open".
 */

import { isCpoHost } from './appHost';

// Must match KILL and CACHE in public/sw.js (sw.test.js checks).
export const SW_KILL = false;
export const SHELL_CACHE_NAME = 'amphive-shell-v1';
const SHELL_KEY = '/index.html';
const RELOAD_GUARD_MS = 30_000;

// One reload per guard key per 30 s, so a reload can never loop. Without
// sessionStorage there is no guard, so no reload either.
export function claimReload(key) {
  try {
    const last = Number(sessionStorage.getItem(key));
    if (last && Date.now() - last < RELOAD_GUARD_MS) return false;
    sessionStorage.setItem(key, String(Date.now()));
    return true;
  } catch {
    return false;
  }
}

/** Drop the cached shell so the next load goes to the network. */
export async function dropShell() {
  try {
    if (await globalThis.caches.has(SHELL_CACHE_NAME)) {
      await (await globalThis.caches.open(SHELL_CACHE_NAME)).delete(SHELL_KEY);
    }
  } catch {
    // No Cache Storage here (insecure context, blocked site data).
  }
}

async function registerShell(sw) {
  await sw.register('/sw.js', { updateViaCache: 'none' });
  const reg = await sw.ready;
  reg.active?.postMessage({ type: 'revalidate' });
}

// Kill switch: stop registering, and unregister every worker that is not
// carrying a push subscription (push users keep a cache-less, fetch-less one).
async function unregisterUnlessPush(sw) {
  for (const reg of await sw.getRegistrations()) {
    const sub = await reg.pushManager?.getSubscription().catch(() => null);
    if (!sub) await reg.unregister();
  }
  try {
    await globalThis.caches.delete(SHELL_CACHE_NAME);
  } catch {
    // No Cache Storage.
  }
}

export function setupServiceWorker({ prod = import.meta.env.PROD, kill = SW_KILL } = {}) {
  // An old shell can ask for a lazy chunk a deploy removed: drop the shell
  // and reload once onto the current app. ErrorBoundary is the fallback.
  window.addEventListener('vite:preloadError', (event) => {
    if (!claimReload('amphive_chunk_reload')) return;
    event.preventDefault();
    dropShell().finally(() => window.location.reload());
  });

  let sw;
  try {
    sw = navigator.serviceWorker; // throws SecurityError with site data blocked
  } catch {
    return;
  }
  if (!prod || !sw || !window.isSecureContext || isCpoHost()) return;

  sw.addEventListener('message', (event) => {
    const type = event.data?.type;
    if ((type === 'reload' || type === 'sw-killed') && claimReload('amphive_sw_reload')) {
      window.location.reload();
    }
  });
  window.addEventListener('load', () => {
    (kill ? unregisterUnlessPush(sw) : registerShell(sw)).catch(() => {});
  });
}
