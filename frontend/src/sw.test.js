/**
 * public/sw.js under a stubbed worker global: which requests it answers,
 * how the shell is swapped and pruned, the kill switch, and that its
 * duplicated constants and route list agree with swControl.js, nginx.conf
 * and App.jsx. The script runs as-is (string-replaced for the KILL and CPO
 * cases), like nginxRoutes.test.js reads the real files.
 */
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

function frontendDir() {
  let dir = process.cwd();
  for (let i = 0; i < 6; i += 1) {
    if (existsSync(join(dir, 'public', 'sw.js')) && existsSync(join(dir, 'nginx.conf'))) return dir;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return resolve(process.cwd(), 'frontend');
}
const FRONTEND = frontendDir();
const SW_SRC = readFileSync(join(FRONTEND, 'public', 'sw.js'), 'utf8');
const ORIGIN = 'https://amphive.app';
const DAY = 24 * 60 * 60 * 1000;

const html = (entry = 'index-AAA.js', extra = '') =>
  `<!doctype html><html><head>
<script type="module" crossorigin src="/assets/${entry}"></script>
<link rel="modulepreload" crossorigin href="/assets/jsx-runtime-X.js">
<link rel="stylesheet" crossorigin href="/assets/index-C.css">${extra}
</head><body><div id="root"></div></body></html>`;

const htmlRes = (body, init = {}) =>
  new Response(body, { status: 200, headers: { 'content-type': 'text/html', 'content-security-policy': "default-src 'self'" }, ...init });

const pathOf = (r) => new URL(typeof r === 'string' ? r : r.url, ORIGIN).pathname;

function fakeCaches(fetchFn) {
  const store = new Map();
  const make = () => {
    const m = new Map();
    return {
      m,
      match: async (r) => m.get(pathOf(r))?.clone(),
      put: async (r, res) => void m.set(pathOf(r), res),
      delete: async (r) => m.delete(pathOf(r)),
      keys: async () => [...m.keys()].map((p) => ({ url: ORIGIN + p })),
      addAll: async (urls) => {
        const rs = await Promise.all(urls.map((u) => fetchFn(u)));
        if (rs.some((r) => !r.ok)) throw new TypeError('addAll failed');
        urls.forEach((u, i) => m.set(pathOf(u), rs[i]));
      },
    };
  };
  return {
    store,
    open: async (name) => {
      if (!store.has(name)) store.set(name, make());
      return store.get(name);
    },
    keys: async () => [...store.keys()],
    has: async (name) => store.has(name),
    delete: async (name) => store.delete(name),
  };
}

function loadSw({ kill = false, hostname = 'amphive.app' } = {}) {
  const src = kill ? SW_SRC.replace('const KILL = false;', 'const KILL = true;') : SW_SRC;
  const listeners = {};
  const clients = [{ postMessage: vi.fn() }];
  const self = {
    addEventListener: (type, fn) => {
      listeners[type] = fn;
    },
    location: new URL(`https://${hostname}/sw.js`),
    fetch: vi.fn(async (r) => {
      const p = pathOf(r);
      if (p === '/index.html') return htmlRes(html());
      if (p.startsWith('/assets/')) return new Response('x', { status: 200 });
      return new Response('net', { status: 200, headers: { 'content-type': 'text/html' } });
    }),
    Response,
    Headers,
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => {}), matchAll: vi.fn(async () => clients) },
    registration: {},
  };
  self.caches = fakeCaches((u) => self.fetch(u));
  new Function('self', src)(self);
  return { self, listeners, clients, caches: self.caches };
}

function fetchEvent(path, { mode = 'cors', method = 'GET', origin = ORIGIN } = {}) {
  const ev = {
    request: { url: origin + path, method, mode },
    waits: [],
    respondWith: vi.fn((p) => {
      ev.response = Promise.resolve(p);
    }),
    waitUntil: vi.fn((p) => ev.waits.push(p)),
  };
  return ev;
}
const lifecycle = async (sw, type, data) => {
  const ev = { data, waits: [], waitUntil: (p) => ev.waits.push(p) };
  sw.listeners[type](ev);
  await Promise.all(ev.waits);
  return ev;
};
const shellText = async (sw) => {
  const r = await (await sw.caches.open('amphive-shell-v1')).match('/index.html');
  return r ? r.text() : null;
};
const assetPaths = async (sw) => [...(await sw.caches.open('amphive-shell-v1')).m.keys()].filter((p) => p.startsWith('/assets/')).sort();

describe('sw.js fetch routing', () => {
  it('never answers API, socket, OAuth, token-landing, auth, unknown, cross-origin or non-GET requests', () => {
    const sw = loadSw();
    const cases = [
      fetchEvent('/api/x'),
      fetchEvent('/api/x', { mode: 'navigate' }),
      fetchEvent('/socket.io/?EIO=4&transport=polling'),
      fetchEvent('/auth/google/callback?code=1', { mode: 'navigate' }),
      fetchEvent('/reset-password?token=t', { mode: 'navigate' }),
      fetchEvent('/verify-email?token=t', { mode: 'navigate' }),
      fetchEvent('/login', { mode: 'navigate' }),
      fetchEvent('/cpo/dashboard', { mode: 'navigate' }),
      fetchEvent('/admin', { mode: 'navigate' }),
      fetchEvent('/privacy', { mode: 'navigate' }),
      fetchEvent('/nope', { mode: 'navigate' }),
      fetchEvent('/sw.js'),
      fetchEvent('/assets/a.js', { origin: 'https://evil.example' }),
      fetchEvent('/assets/a.js', { method: 'POST' }),
    ];
    cases.forEach((ev) => sw.listeners.fetch(ev));
    cases.forEach((ev) => expect(ev.respondWith, ev.request.url).not.toHaveBeenCalled());
  });

  it('registers no fetch handler on the cpo. host', () => {
    expect(loadSw({ hostname: 'cpo.amphive.app' }).listeners.fetch).toBeUndefined();
  });

  it('serves the cached shell for a navigation to /session and revalidates', async () => {
    const sw = loadSw();
    await lifecycle(sw, 'install');
    sw.self.fetch.mockClear();
    const ev = fetchEvent('/session', { mode: 'navigate' });
    sw.listeners.fetch(ev);
    const res = await ev.response;
    expect(await res.text()).toBe(html());
    expect(res.headers.get('content-security-policy')).toBe("default-src 'self'");
    expect(ev.waitUntil).toHaveBeenCalled();
    await Promise.all(ev.waits);
    expect(sw.self.fetch).toHaveBeenCalledWith('/index.html', { cache: 'no-store' });
  });

  it('falls back to the network on a cache miss and on a rejected match', async () => {
    const sw = loadSw();
    const miss = fetchEvent('/', { mode: 'navigate' });
    sw.listeners.fetch(miss);
    expect(await (await miss.response).text()).toBe('net');

    sw.caches.open = vi.fn().mockRejectedValue(new Error('boom'));
    const broken = fetchEvent('/credit', { mode: 'navigate' });
    sw.listeners.fetch(broken);
    expect(await (await broken.response).text()).toBe('net');
  });

  it('goes network-first for a shell older than 7 days, cache if the network fails', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
      const sw = loadSw();
      await lifecycle(sw, 'install');
      vi.setSystemTime(Date.now() + 8 * DAY);
      const fresh = fetchEvent('/', { mode: 'navigate' });
      sw.listeners.fetch(fresh);
      expect(await (await fresh.response).text()).toBe('net');

      sw.self.fetch.mockRejectedValue(new TypeError('offline'));
      const offline = fetchEvent('/', { mode: 'navigate' });
      sw.listeners.fetch(offline);
      expect(await (await offline.response).text()).toBe(html());
    } finally {
      vi.useRealTimers();
    }
  });

  it('caches assets cache-first and does not cache a 404', async () => {
    const sw = loadSw();
    const ev = fetchEvent('/assets/Dashboard-1.js');
    sw.listeners.fetch(ev);
    await (await ev.response).text();
    await Promise.all(ev.waits);
    expect(await assetPaths(sw)).toContain('/assets/Dashboard-1.js');
    sw.self.fetch.mockClear();
    const again = fetchEvent('/assets/Dashboard-1.js');
    sw.listeners.fetch(again);
    await again.response;
    expect(sw.self.fetch).not.toHaveBeenCalled();

    sw.self.fetch.mockResolvedValueOnce(new Response('gone', { status: 404 }));
    const gone = fetchEvent('/assets/old.js');
    sw.listeners.fetch(gone);
    expect((await gone.response).status).toBe(404);
    expect(await assetPaths(sw)).not.toContain('/assets/old.js');
  });
});

describe('sw.js swapShell', () => {
  it('stores the shell with its entry assets on install', async () => {
    const sw = loadSw();
    await lifecycle(sw, 'install');
    expect(await shellText(sw)).toBe(html());
    expect(await assetPaths(sw)).toEqual(['/assets/index-AAA.js', '/assets/index-C.css', '/assets/jsx-runtime-X.js']);
  });

  it('keeps the old shell when an entry asset fails to download', async () => {
    const sw = loadSw();
    await lifecycle(sw, 'install');
    const base = sw.self.fetch.getMockImplementation();
    sw.self.fetch.mockImplementation(async (r) => {
      const p = pathOf(r);
      if (p === '/index.html') return htmlRes(html('index-BBB.js'));
      if (p === '/assets/index-BBB.js') return new Response('', { status: 404 });
      return base(r);
    });
    await lifecycle(sw, 'message', { type: 'revalidate' });
    expect(await shellText(sw)).toBe(html());
  });

  it('rejects an HTML 200 that is not the app shell', async () => {
    const sw = loadSw();
    sw.self.fetch.mockImplementation(async () => htmlRes('<html><body>Checking your browser</body></html>'));
    await lifecycle(sw, 'install');
    expect(await shellText(sw)).toBeNull();
  });

  it('restamps an unchanged shell, and prunes old assets one swap later', async () => {
    const sw = loadSw();
    await lifecycle(sw, 'install');
    const stamp = async () =>
      (await (await sw.caches.open('amphive-shell-v1')).match('/index.html')).headers.get('x-shell-fetched-at');
    const first = await stamp();
    await new Promise((r) => setTimeout(r, 5));
    await lifecycle(sw, 'message', { type: 'revalidate' });
    expect(Number(await stamp())).toBeGreaterThan(Number(first));

    const base = sw.self.fetch.getMockImplementation();
    const deploy = (entry) =>
      sw.self.fetch.mockImplementation(async (r) => (pathOf(r) === '/index.html' ? htmlRes(html(entry)) : base(r)));
    deploy('index-BBB.js');
    await lifecycle(sw, 'message', { type: 'revalidate' });
    expect(await shellText(sw)).toBe(html('index-BBB.js'));
    // The old entry survives the swap that replaced it...
    expect(await assetPaths(sw)).toContain('/assets/index-AAA.js');
    deploy('index-CCC.js');
    await lifecycle(sw, 'message', { type: 'revalidate' });
    // ...and goes on the next one.
    expect(await assetPaths(sw)).not.toContain('/assets/index-AAA.js');
    expect(await assetPaths(sw)).toContain('/assets/index-BBB.js');
  });
});

describe('sw.js activate and kill switch', () => {
  it('a first install posts no reload; dropping an older shell cache does', async () => {
    const sw = loadSw();
    await lifecycle(sw, 'activate');
    expect(sw.clients[0].postMessage).not.toHaveBeenCalled();
    await sw.caches.open('amphive-shell-v0');
    await lifecycle(sw, 'activate');
    expect(await sw.caches.has('amphive-shell-v0')).toBe(false);
    expect(sw.clients[0].postMessage).toHaveBeenCalledWith({ type: 'reload' });
  });

  it('KILL with no cache to drop posts nothing (enabling push is not interrupted)', async () => {
    const sw = loadSw({ kill: true });
    await lifecycle(sw, 'activate');
    expect(sw.clients[0].postMessage).not.toHaveBeenCalled();
  });

  it('KILL: no fetch handler, no install fetch, caches dropped, pages told', async () => {
    const sw = loadSw({ kill: true });
    expect(sw.listeners.fetch).toBeUndefined();
    await lifecycle(sw, 'install');
    expect(sw.self.fetch).not.toHaveBeenCalled();
    await sw.caches.open('amphive-shell-v1');
    await lifecycle(sw, 'activate');
    expect(await sw.caches.keys()).toEqual([]);
    expect(sw.clients[0].postMessage).toHaveBeenCalledWith({ type: 'sw-killed' });
    // Push still handled.
    expect(sw.listeners.push).toBeTypeOf('function');
  });
});

describe('sw.js constants agree with the rest of the app', () => {
  const control = readFileSync(join(FRONTEND, 'src', 'utils', 'swControl.js'), 'utf8');
  const nginx = readFileSync(join(FRONTEND, 'nginx.conf'), 'utf8');
  const app = readFileSync(join(FRONTEND, 'src', 'App.jsx'), 'utf8');

  it('KILL and the cache name match swControl.js', () => {
    expect(SW_SRC.match(/const KILL = (true|false);/)[1]).toBe(control.match(/export const SW_KILL = (true|false);/)[1]);
    expect(SW_SRC.match(/const CACHE = '([^']+)';/)[1]).toBe(control.match(/export const SHELL_CACHE_NAME = '([^']+)';/)[1]);
    expect(SW_SRC).toContain("const SHELL_KEY = '/index.html';");
    expect(control).toContain("const SHELL_KEY = '/index.html';");
  });

  it('SHELL_ROUTES is a subset of the nginx allowlist and of App.jsx routes, without auth or operator routes', () => {
    const shell = SW_SRC.match(/const SHELL_ROUTES = \/\^\\\/\(\?:\$\|\(\?:([^)]+)\)/)[1].split('|');
    const allow = nginx.match(/location\s+~\s+\^\/\(\?:\$\|\(\?:([^)]+)\)/)[1].split('|');
    expect(shell.length).toBeGreaterThan(5);
    for (const seg of shell) {
      expect(allow).toContain(seg);
      expect(app).toContain(`path="/${seg}"`);
    }
    for (const seg of ['auth', 'reset-password', 'verify-email', 'login', 'signup', 'forgot-password', 'cpo', 'admin']) {
      expect(shell).not.toContain(seg);
    }
  });
});
