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
  },
})
