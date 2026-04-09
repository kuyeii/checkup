import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        // Use 127.0.0.1 to avoid IPv6 localhost (::1) connection issues on some machines.
        // Override if needed:
        //   VITE_API_TARGET=http://127.0.0.1:8000 npm run dev
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        secure: false
      }
    }
  }
})
