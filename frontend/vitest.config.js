import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.js'],
    // A real origin (not about:blank's opaque one) so cookies/storage behave.
    environmentOptions: { jsdom: { url: 'http://localhost/' } },
  },
})
