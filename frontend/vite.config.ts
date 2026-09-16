import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// API_PORT lets make dev move the backend off a busy :8000.
const apiPort = process.env.API_PORT || '8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // 127.0.0.1: Vite would otherwise bind IPv6-only, which some headless browsers cannot reach.
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${apiPort}`,
        changeOrigin: true,
      },
    },
  },
})
