/**
 * Hostname partition helpers (driver vs. CPO operator portal).
 * ============================================================
 * One bundle is served on two hostnames: the driver app on the bare domain
 * (e.g. amphive.duckdns.org) and the CPO operator portal on the "cpo."
 * subdomain (cpo.amphive.duckdns.org — DuckDNS serves subdomains of the
 * registered host at the same IP). App.jsx and Navbar.jsx branch on
 * isCpoHost() so each host shows only its own experience.
 *
 * For local dev / tests, set VITE_FORCE_CPO_HOST=true (or "1") to force the
 * CPO experience regardless of hostname. Every helper also accepts an
 * explicit hostname argument so unit tests don't need to mock
 * window.location.
 */

const CPO_PREFIX = 'cpo.';

/** True when the app should render the CPO operator portal experience. */
export const isCpoHost = (hostname = window.location.hostname) => {
  const forced = import.meta.env.VITE_FORCE_CPO_HOST;
  if (forced !== undefined && forced !== '') {
    return forced === true || forced === 'true' || forced === '1';
  }
  return hostname.startsWith(CPO_PREFIX);
};

const originFor = (host) => {
  const { protocol, port } = window.location;
  return `${protocol}//${host}${port ? `:${port}` : ''}`;
};

/** Origin of the driver app (strips a leading "cpo." if present). */
export const driverOrigin = (hostname = window.location.hostname) =>
  originFor(hostname.startsWith(CPO_PREFIX) ? hostname.slice(CPO_PREFIX.length) : hostname);

/** Origin of the CPO operator portal (prepends "cpo." if not already there). */
export const cpoOrigin = (hostname = window.location.hostname) =>
  originFor(hostname.startsWith(CPO_PREFIX) ? hostname : `${CPO_PREFIX}${hostname}`);
