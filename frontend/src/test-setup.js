// Vitest setup — jest-dom matchers + RTL cleanup between tests (the
// automatic cleanup only registers itself when vitest globals are enabled,
// and this project imports test APIs explicitly instead).
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom doesn't implement scrollIntoView (used by Home's QR/deep-link
// prefill to bring the Start Charging card into view) — stub it so any
// component calling it doesn't crash under test.
if (!window.HTMLElement.prototype.scrollIntoView) {
  window.HTMLElement.prototype.scrollIntoView = () => {};
}

afterEach(() => cleanup());
