/**
 * loadRazorpay() tests (utils/razorpay.js): resolves immediately when the
 * SDK is already on window, otherwise injects the checkout.js <script> once
 * and resolves/rejects based on its load outcome. The loader caches an
 * in-flight promise on the module, so each test re-imports the module fresh
 * (vi.resetModules) to avoid bleeding state between the load/error cases.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const RAZORPAY_SRC = 'https://checkout.razorpay.com/v1/checkout.js';

const getScript = () => document.querySelector(`script[src="${RAZORPAY_SRC}"]`);
const getScripts = () => document.querySelectorAll(`script[src="${RAZORPAY_SRC}"]`);

beforeEach(() => {
  vi.resetModules();
  delete window.Razorpay;
});

afterEach(() => {
  getScripts().forEach((el) => el.remove());
});

describe('loadRazorpay — already loaded', () => {
  it('resolves immediately with window.Razorpay and injects no script', async () => {
    const fakeRazorpay = function FakeRazorpay() {};
    window.Razorpay = fakeRazorpay;
    const { loadRazorpay } = await import('./razorpay');

    await expect(loadRazorpay()).resolves.toBe(fakeRazorpay);
    expect(getScript()).toBeNull();
  });
});

describe('loadRazorpay — script injection', () => {
  it('injects the checkout.js script once (reusing the in-flight promise) and resolves with window.Razorpay on load', async () => {
    const { loadRazorpay } = await import('./razorpay');

    const promise = loadRazorpay();
    const script = getScript();
    expect(script).not.toBeNull();
    expect(script.async).toBe(true);

    // A second call before load fires must reuse the same in-flight promise
    // instead of injecting a duplicate <script> tag.
    loadRazorpay();
    expect(getScripts()).toHaveLength(1);

    const fakeRazorpay = function FakeRazorpay() {};
    window.Razorpay = fakeRazorpay;
    script.onload();

    await expect(promise).resolves.toBe(fakeRazorpay);
  });

  it('rejects with a friendly message and removes the script on error, allowing a later retry', async () => {
    const { loadRazorpay } = await import('./razorpay');

    const promise = loadRazorpay();
    const script = getScript();
    expect(script).not.toBeNull();

    script.onerror();

    await expect(promise).rejects.toThrow(
      "Couldn't load the payment window. Check your connection and try again."
    );
    // Failure is not cached: the failed script is removed from the DOM...
    expect(getScript()).toBeNull();
    // ...and a later call injects a fresh one rather than re-returning the
    // rejected promise.
    loadRazorpay();
    expect(getScripts()).toHaveLength(1);
  });
});
