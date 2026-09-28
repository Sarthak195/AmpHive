import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    // No manualChunks (measured 2026-09-28, reverting the 2026-08-18 change
    // below): naming recharts+victory-vendor and leaflet+react-leaflet into
    // their own 'vendor-charts'/'vendor-maps' chunks made CpoDashboard's and
    // MapPage's edits cheaper to re-download (the original goal), but it had
    // a side effect nobody caught — those libraries need a full CJS
    // react-dom (portals), and main.jsx (the entry) needs that same
    // react-dom too. With no forced chunk, Rolldown puts that shared
    // react-dom in its own small automatic chunk, same as it already does
    // for react/jsx-runtime. Once a manual chunk exists to hold it instead,
    // Rolldown resolves the resulting import cycle by folding the ENTIRE
    // forced chunk into the entry's static import graph — so every page,
    // including the public marketing/login/signup pages that never render a
    // chart or a map, was shipping a <link rel="modulepreload"> for the full
    // recharts bundle (113 kB gzip) on every load. Confirmed by removing
    // each manual-chunk rule independently: whichever one remained is the
    // one that leaked into dist/index.html's preload list.
    //
    // recharts and leaflet are only ever reached via the existing
    // React.lazy() routes in App.jsx (CpoDashboard, MapPage), so without
    // manualChunks they land entirely inside those routes' own chunks —
    // still never downloaded until an operator/driver actually opens that
    // page. The tradeoff is the one the 2026-08-18 comment called out in
    // reverse: an unrelated edit to CpoDashboard.jsx once again busts the
    // cache for its bundled copy of recharts. That's the right side to be
    // wrong on — CpoDashboard is behind an operator login the India-latency
    // problem doesn't reach, and every anonymous visit to amphive.app was
    // paying the recharts tax under the old setup.
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
