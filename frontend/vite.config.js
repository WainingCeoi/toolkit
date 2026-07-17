import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the FastAPI backend, so the frontend calls same-origin
// (/api/...) and EventSource/SSE works without CORS in dev. `host: '127.0.0.1'` avoids Vite
// binding IPv6-only ([::1]), which some in-app / headless browsers can't reach.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
