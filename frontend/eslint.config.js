import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

// Flat config. The react-hooks plugin is the point: it catches the
// stale-closure / missing-cleanup / conditional-hook mistakes that a plain
// build never surfaces.
//
// This file stays .js on purpose: it is what configures TypeScript linting,
// so keeping it plain JS avoids a bootstrap dependency on the thing it sets up.
//
// Parsing only — no type-aware rules. typescript-eslint reads the compiler API
// from the `typescript` package (v6 here); the authoritative type check is a
// separate TypeScript 7 pass, `npm run typecheck`. See tsconfig.json.
export default [
  { ignores: ['dist/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended.map((cfg) => ({
    ...cfg,
    files: ['src/**/*.{ts,tsx}'],
  })),
  // Node-context config files (vite/eslint config, run under Node).
  {
    files: ['*.config.{js,ts}', 'eslint.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: { ...globals.browser },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      react,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      // Mark JSX-referenced identifiers (component variables) as used.
      'react/jsx-uses-vars': 'error',
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Pre-dates this rule landing in eslint-plugin-react-hooks v7 and fires on
      // three existing load-on-mount effects (DepUpgrade, Remux, Subscription).
      // Warn, not error, so the lint gate can be switched on without bundling a
      // behaviour change into the TypeScript migration. Tracked separately.
      'react-hooks/set-state-in-effect': 'warn',
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]', argsIgnorePattern: '^_' }],
    },
  },
  {
    // On .ts/.tsx the base rule misfires on type-only constructs (interfaces,
    // overloads, type params), so hand off to the TS-aware equivalent.
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { varsIgnorePattern: '^[A-Z_]', argsIgnorePattern: '^_' },
      ],
    },
  },
  {
    files: ['src/**/*.test.{ts,tsx}'],
    languageOptions: {
      globals: {
        ...globals.browser,
        describe: 'readonly',
        it: 'readonly',
        test: 'readonly',
        expect: 'readonly',
        vi: 'readonly',
        beforeEach: 'readonly',
        afterEach: 'readonly',
      },
    },
  },
]
