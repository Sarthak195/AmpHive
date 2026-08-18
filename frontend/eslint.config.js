import js from '@eslint/js'
import globals from 'globals'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

// All app code is plain JS/JSX — the TypeScript toolchain was removed
// 2026-07-07 (TD#14). NOTE: the old config only matched **/*.{ts,tsx}, so
// `npm run lint` passed while linting nothing; this one actually covers
// the codebase.
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      jsxA11y.flatConfigs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      // Allow intentionally-unused args/vars prefixed with _
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      // Policy: the codebase uses the classic fetch-on-mount idiom
      // (effect calls a function that setState's loading synchronously) in
      // several pages, and SessionContext stores the socket handle in state.
      // This new react-hooks v7 rule flags all of them; it's a perf hint,
      // not a correctness bug. Revisit alongside a data-fetching refactor.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
  {
    // Vitest specs and the Vite config run in Node, not the browser: without
    // this they trip no-undef on `process` (see nginxRoutes.test.js, which
    // walks up from process.cwd() to find nginx.conf).
    files: ['**/*.test.{js,jsx}', 'src/test-setup.js', '*.config.js'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    // Context modules intentionally export a Provider component plus its
    // useX hook — losing fast-refresh on these three files is acceptable.
    files: ['src/contexts/**/*.jsx'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
