/* global process */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.js',
    // Must exceed test-setup's asyncUtilTimeout (5 s): a waitFor that's
    // still legitimately retrying on a slow CI runner would otherwise be
    // killed by vitest's own 5 s default first.
    testTimeout: 15000,
    // CI-only safety net: a residual timing flake reruns instead of redding
    // the whole frontend-tests job. Local runs stay at 0 so real failures
    // surface immediately in dev rather than being masked by a rerun.
    retry: process.env.CI ? 2 : 0,
  },
})
