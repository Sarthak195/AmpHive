// Vitest setup — jest-dom matchers + RTL cleanup between tests (the
// automatic cleanup only registers itself when vitest globals are enabled,
// and this project imports test APIs explicitly instead).
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup, configure } from '@testing-library/react';

// Shared CI runners are slow enough that the 1 s default for waitFor/findBy
// times out mid-render (CpoPricing/AuthContext were the recurring
// run-failed emails on main). Locally everything still resolves in ms —
// this only widens the ceiling, not the happy path.
configure({ asyncUtilTimeout: 5000 });

// jsdom doesn't implement scrollIntoView (used by Home's QR/deep-link
// prefill to bring the Start Charging card into view) — stub it so any
// component calling it doesn't crash under test.
if (!window.HTMLElement.prototype.scrollIntoView) {
  window.HTMLElement.prototype.scrollIntoView = () => {};
}

afterEach(() => cleanup());
