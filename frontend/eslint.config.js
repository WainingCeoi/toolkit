import js from '@eslint/js'
import globals from 'globals'
import babelParser from '@babel/eslint-parser'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// Flat config. The react-hooks plugin is the point: it catches the
// stale-closure / missing-cleanup / conditional-hook mistakes that a plain
// build never surfaces.
//
// This file stays .js on purpose: it is what configures TypeScript linting, so
// keeping it plain JS avoids a bootstrap dependency on the thing it sets up.
//
// TypeScript is parsed by @babel/eslint-parser, NOT typescript-eslint. That is
// forced, not preferred: TypeScript 7 ships no compiler API (its package
// exports only a version string) and no tsserver, and every published
// typescript-eslint refuses to load against it — keeping it would mean keeping
// a second, older TypeScript installed purely to parse. Babel reads the syntax
// without the compiler, which lets `typescript` here be exactly one version: 7.
//
// The trade is that no rule below is type-aware. Everything needing types is
// enforced by the compiler instead — `npm run typecheck`, which is strict and
// now also owns unused-code detection via noUnusedLocals/noUnusedParameters
// (see tsconfig.json). Syntax-only rules cannot do that job correctly.

// Babel is used purely as a parser, so the TS support is requested as parser
// plugins rather than a preset: @babel/eslint-parser feeds the parser only from
// parserOpts.plugins, and presets (being transforms) never reach it.
//
// jsx is enabled only for .tsx. In a .ts file `<T>expr` is a type assertion,
// but with jsx on it parses as an element — so enabling it everywhere would
// silently mis-parse valid TypeScript.
const tsLanguageOptions = (plugins) => ({
  ecmaVersion: 2023,
  sourceType: 'module',
  globals: { ...globals.browser },
  parser: babelParser,
  parserOptions: {
    // No babel config in this project: Vite compiles via esbuild and Babel is
    // here only for the linter, so there is no second build config to drift.
    requireConfigFile: false,
    babelOptions: { babelrc: false, configFile: false, parserOpts: { plugins } },
  },
})

const tsRules = {
  // Mark JSX-referenced identifiers (component variables) as used.
  'react/jsx-uses-vars': 'error',
  ...reactHooks.configs.recommended.rules,
  'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
  // Off on purpose, and not a gap: the base rule counts only value references,
  // so every `import type` and every identifier used solely in a type position
  // reads as unused. tsc's noUnusedLocals/noUnusedParameters cover this
  // correctly because they actually resolve types.
  'no-unused-vars': 'off',
  // Same reason: Babel emits type annotations as nodes the core scope analyser
  // does not recognise as types, so no-undef misfires on type-only names.
  'no-undef': 'off',
}

const tsPlugins = { react, 'react-hooks': reactHooks, 'react-refresh': reactRefresh }

export default [
  { ignores: ['dist/**'] },
  js.configs.recommended,
  // Node-context config files (vite/eslint config, run under Node).
  {
    files: ['*.config.{js,ts}', 'eslint.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ['src/**/*.ts'],
    languageOptions: tsLanguageOptions(['typescript']),
    plugins: tsPlugins,
    rules: tsRules,
  },
  {
    files: ['src/**/*.tsx'],
    languageOptions: tsLanguageOptions(['typescript', 'jsx']),
    plugins: tsPlugins,
    rules: tsRules,
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
