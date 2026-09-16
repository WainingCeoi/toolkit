import js from '@eslint/js'
import globals from 'globals'
import babelParser from '@babel/eslint-parser'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// @babel/eslint-parser, not typescript-eslint: TypeScript 7 has no compiler API for it to load.
// Plugins go in parserOpts.plugins: @babel/eslint-parser ignores presets.
// jsx only for .tsx: in a .ts file `<T>expr` is a type assertion and would mis-parse.
const tsLanguageOptions = (plugins) => ({
  ecmaVersion: 2023,
  sourceType: 'module',
  globals: { ...globals.browser },
  parser: babelParser,
  parserOptions: {
    // No babel config exists; Vite compiles via esbuild.
    requireConfigFile: false,
    babelOptions: { babelrc: false, configFile: false, parserOpts: { plugins } },
  },
})

const tsRules = {
  'react/jsx-uses-vars': 'error',
  ...reactHooks.configs.recommended.rules,
  'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
  // Off because tsc covers them: Babel has no type info, so both misfire on type-only names.
  'no-unused-vars': 'off',
  'no-undef': 'off',
}

const tsPlugins = { react, 'react-hooks': reactHooks, 'react-refresh': reactRefresh }

export default [
  { ignores: ['dist/**'] },
  js.configs.recommended,
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
