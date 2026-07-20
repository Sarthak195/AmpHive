/**
 * Hostname-partition helper tests (utils/appHost.js): cpo.-prefix detection,
 * the VITE_FORCE_CPO_HOST dev/test override, and counterpart-origin derivation.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { isCpoHost, isSplitHost, cpoOrigin, driverOrigin } from './appHost';

// jsdom supplies protocol/port; derive expectations from it rather than
// hardcoding the test-runner URL.
const origin = (host) => {
  const { protocol, port } = window.location;
  return `${protocol}//${host}${port ? `:${port}` : ''}`;
};

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('isCpoHost', () => {
  it('is false on the bare driver hostname', () => {
    expect(isCpoHost('amphive.duckdns.org')).toBe(false);
  });

  it('is true when the hostname starts with "cpo."', () => {
    expect(isCpoHost('cpo.amphive.duckdns.org')).toBe(true);
  });

  it('honors the VITE_FORCE_CPO_HOST override regardless of hostname', () => {
    vi.stubEnv('VITE_FORCE_CPO_HOST', 'true');
    expect(isCpoHost('localhost')).toBe(true);
    vi.stubEnv('VITE_FORCE_CPO_HOST', 'false');
    expect(isCpoHost('cpo.localhost')).toBe(false);
  });

  it('defaults to the real hostname (jsdom localhost → driver host)', () => {
    expect(isCpoHost()).toBe(false);
  });
});

describe('isSplitHost — bare-IP/localhost stay unsplit', () => {
  it('is true for real domains, cpo. or not', () => {
    expect(isSplitHost('amphive.duckdns.org')).toBe(true);
    expect(isSplitHost('cpo.amphive.duckdns.org')).toBe(true);
  });

  it('is false for bare IPs (DNS-outage fallback) and localhost', () => {
    expect(isSplitHost('8.231.81.12')).toBe(false);
    expect(isSplitHost('localhost')).toBe(false);
  });

  it('origins collapse to same-host on unsplit hosts (internal /cpo)', () => {
    expect(cpoOrigin('8.231.81.12')).toBe(origin('8.231.81.12'));
    expect(driverOrigin('8.231.81.12')).toBe(origin('8.231.81.12'));
    expect(cpoOrigin('localhost')).toBe(origin('localhost'));
  });
});

describe('cpoOrigin / driverOrigin', () => {
  it('derives the CPO origin from a driver hostname', () => {
    expect(cpoOrigin('amphive.duckdns.org')).toBe(origin('cpo.amphive.duckdns.org'));
  });

  it('is a no-op when already on the CPO hostname', () => {
    expect(cpoOrigin('cpo.amphive.duckdns.org')).toBe(origin('cpo.amphive.duckdns.org'));
  });

  it('derives the driver origin from a CPO hostname', () => {
    expect(driverOrigin('cpo.amphive.duckdns.org')).toBe(origin('amphive.duckdns.org'));
  });

  it('is a no-op when already on the driver hostname', () => {
    expect(driverOrigin('amphive.duckdns.org')).toBe(origin('amphive.duckdns.org'));
  });
});
