/**
 * nginx SPA route allowlist ↔ App.jsx guard
 * =========================================
 * frontend/nginx.conf answers HTTP 200 only for paths whose first segment is
 * on an explicit allowlist, and serves everything else as a real 404 (with the
 * React NotFound page as the body). That kills the soft-404 problem — an SPA
 * fallback that returns 200 for literally every URL, which tells a crawler the
 * site has unlimited pages that are all duplicates of the homepage.
 *
 * The cost of that fix is a list of route names duplicated in a file the
 * frontend build never reads. Adding `<Route path="/rewards" …>` to App.jsx
 * without touching nginx.conf works perfectly in `npm run dev` (Vite's dev
 * server has its own fallback) and 404s in production — the worst possible
 * failure shape: invisible locally, visible only to users.
 *
 * So this test is the interlock. It parses the allowlist out of the real
 * nginx.conf and every route path out of the real App.jsx, and fails if
 * App.jsx knows a top-level segment that nginx does not (or vice versa — a
 * stale allowlist entry is a smaller problem, but still a lie about what the
 * app serves). No mocks, no fixtures: if the assertion passes, the two files
 * on disk actually agree.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Locate the `frontend/` directory from the test runner's cwd.
 *
 * `import.meta.url` is NOT usable here: under vitest's jsdom environment it
 * resolves to an http://localhost/… URL, not a file: one, so readFileSync
 * rejects it ("The URL must be of scheme file"). Walking up from cwd until we
 * find a directory holding BOTH files keeps the test working whether it is
 * run from frontend/ (npm test) or from the repo root.
 */
function frontendDir() {
  let dir = process.cwd();
  for (let i = 0; i < 6; i += 1) {
    if (existsSync(join(dir, 'nginx.conf')) && existsSync(join(dir, 'src', 'App.jsx'))) {
      return dir;
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  // Last resort: the conventional layout, so the failure message names a path.
  return resolve(process.cwd(), 'frontend');
}

const FRONTEND = frontendDir();
const nginxConf = readFileSync(join(FRONTEND, 'nginx.conf'), 'utf8');
const appSource = readFileSync(join(FRONTEND, 'src', 'App.jsx'), 'utf8');

/**
 * Pull the alternation out of nginx.conf's allowlist location, which looks
 * like:
 *   location ~ ^/(?:$|(?:account|activity|…|wallet)(?:/|$)) {
 * The `$` alternative is the site root and has no segment name, so it sits
 * outside the captured group.
 */
function nginxAllowlist() {
  const match = nginxConf.match(/location\s+~\s+\^\/\(\?:\$\|\(\?:([^)]+)\)/);
  if (!match) {
    throw new Error(
      'Could not find the SPA route allowlist location in frontend/nginx.conf. ' +
        'If the regex was reshaped, update this parser too — do not delete the test.',
    );
  }
  return match[1].split('|').map((s) => s.trim()).filter(Boolean);
}

/**
 * Every route path App.jsx declares. Two sources, because the file expresses
 * routes two ways:
 *   1. `path="/map"` literals on <Route> elements — the overwhelming majority.
 *   2. the CPO-host array of driver-only paths that are `.map()`ed into
 *      <Route path={path} …> redirect stubs, where the literal is a quoted
 *      string in an array rather than a JSX attribute.
 * Missing (2) would not currently lose coverage (every path in it also appears
 * as a literal elsewhere), but relying on that coincidence is how a guard test
 * quietly stops guarding.
 */
function appRoutePaths() {
  const paths = new Set();

  for (const m of appSource.matchAll(/path="([^"]+)"/g)) {
    paths.add(m[1]);
  }

  const arrayBlock = appSource.match(/\[([^\]]*?)\]\.map\(\(path\)/);
  if (arrayBlock) {
    for (const m of arrayBlock[1].matchAll(/'([^']+)'/g)) {
      paths.add(m[1]);
    }
  }

  return [...paths];
}

/** "/admin/tenants/:id" -> "admin"; "/" and "*" have no segment. */
function firstSegment(routePath) {
  if (routePath === '*' || routePath === '/') return null;
  const seg = routePath.replace(/^\//, '').split('/')[0];
  // "/cpo/*" -> "cpo"; a bare "*" segment carries no name.
  return seg && seg !== '*' ? seg : null;
}

describe('nginx SPA route allowlist', () => {
  const allowlist = nginxAllowlist();

  it('parses a non-trivial allowlist out of nginx.conf', () => {
    // Sanity check on the parser itself: if the regex above ever matches
    // something degenerate, the two assertions below would pass vacuously.
    expect(allowlist.length).toBeGreaterThan(5);
    expect(allowlist).toContain('map');
    expect(allowlist).toContain('cpo');
  });

  it('covers every top-level route segment declared in App.jsx', () => {
    const segments = [...new Set(appRoutePaths().map(firstSegment).filter(Boolean))].sort();
    const missing = segments.filter((s) => !allowlist.includes(s));

    expect(
      missing,
      `App.jsx declares route segment(s) that frontend/nginx.conf does not allow, so they ` +
        `would be served with a 404 status in production: ${missing.join(', ')}. ` +
        `Add them to the "location ~ ^/(?:$|(?:…))" alternation in frontend/nginx.conf.`,
    ).toEqual([]);
  });

  it('has no stale entries that App.jsx no longer routes', () => {
    const segments = new Set(appRoutePaths().map(firstSegment).filter(Boolean));
    const stale = allowlist.filter((s) => !segments.has(s));

    expect(
      stale,
      `frontend/nginx.conf allowlists segment(s) App.jsx no longer routes: ${stale.join(', ')}. ` +
        `These now return 200 with the SPA shell for a path the router sends to NotFound — ` +
        `remove them from the nginx alternation.`,
    ).toEqual([]);
  });

  it('serves the site root, which the router declares as "/"', () => {
    // The root is the `$` alternative in the nginx regex, not a named
    // segment — assert it explicitly so a refactor cannot drop it silently.
    expect(appRoutePaths()).toContain('/');
    expect(nginxConf).toMatch(/location\s+~\s+\^\/\(\?:\$\|/);
  });
});
