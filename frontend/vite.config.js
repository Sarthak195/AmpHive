import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Measured on the pre-change build (2026-08-18): CpoDashboard shipped
        // a 398 kB route chunk because recharts (plus its redux/immer/d3
        // baggage) was inlined into the single route that imports it, and
        // MapPage a 163 kB chunk for leaflet + react-leaflet. Both libraries
        // change only when we bump them, but their content hash was tied to
        // the page that owned them — so editing one dashboard tile made every
        // returning operator re-download all of recharts.
        //
        // Two named vendor chunks, no more: they are the only dependencies
        // large enough for the extra request to pay for itself, and both are
        // already behind a lazy route so nobody downloads them until they
        // land on that page.
        //
        // Function form rather than the `{ name: [pkgs] }` shorthand because
        // Vite 8's rolldown bundler only accepts a function here.
        manualChunks: (id) => {
          const path = id.replace(/\\/g, '/');
          if (!path.includes('/node_modules/')) return null;
          if (/\/node_modules\/(recharts|victory-vendor)\//.test(path)) return 'vendor-charts';
          if (/\/node_modules\/(leaflet|react-leaflet|@react-leaflet)\//.test(path)) return 'vendor-maps';
          return null;
        },
      },
    },
  },
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
