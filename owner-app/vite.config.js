import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the API runs separately (uvicorn --factory owner.api:create_app --port 8100) and Vite forwards /api to it.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8100', '/get-app': 'http://127.0.0.1:8100' } },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 600 },
})
