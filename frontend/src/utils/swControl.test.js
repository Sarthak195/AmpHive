import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { claimReload, setupServiceWorker } from './swControl';

describe('claimReload', () => {
  beforeEach(() => sessionStorage.clear());

  it('allows one reload per key per 30 s', () => {
    expect(claimReload('k')).toBe(true);
    expect(claimReload('k')).toBe(false);
    sessionStorage.setItem('k', String(Date.now() - 31_000));
    expect(claimReload('k')).toBe(true);
  });

  it('refuses when sessionStorage throws, so a reload can never loop', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(claimReload('k')).toBe(false);
    spy.mockRestore();
  });
});

describe('setupServiceWorker', () => {
  let sw;
  let reg;
  beforeEach(() => {
    sessionStorage.clear();
    reg = { unregister: vi.fn().mockResolvedValue(true), pushManager: { getSubscription: vi.fn().mockResolvedValue(null) } };
    sw = {
      register: vi.fn().mockResolvedValue(reg),
      ready: Promise.resolve({ active: { postMessage: vi.fn() } }),
      getRegistrations: vi.fn().mockResolvedValue([reg]),
      addEventListener: vi.fn(),
    };
    Object.defineProperty(window.navigator, 'serviceWorker', { configurable: true, value: sw });
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  });
  afterEach(() => {
    delete window.navigator.serviceWorker;
  });
  const fireLoad = async () => {
    window.dispatchEvent(new Event('load'));
    await new Promise((r) => setTimeout(r, 0));
  };

  it('registers /sw.js with updateViaCache none in prod', async () => {
    setupServiceWorker({ prod: true, kill: false });
    await fireLoad();
    expect(sw.register).toHaveBeenCalledWith('/sw.js', { updateViaCache: 'none' });
  });

  it('does not register outside prod', async () => {
    setupServiceWorker({ prod: false, kill: false });
    await fireLoad();
    expect(sw.register).not.toHaveBeenCalled();
  });

  it('kill switch: never registers, unregisters a worker without a push subscription', async () => {
    setupServiceWorker({ prod: true, kill: true });
    await fireLoad();
    expect(sw.register).not.toHaveBeenCalled();
    expect(reg.unregister).toHaveBeenCalled();
  });

  it('kill switch keeps a worker that carries a push subscription', async () => {
    reg.pushManager.getSubscription.mockResolvedValue({ endpoint: 'e' });
    setupServiceWorker({ prod: true, kill: true });
    await fireLoad();
    expect(reg.unregister).not.toHaveBeenCalled();
  });
});
