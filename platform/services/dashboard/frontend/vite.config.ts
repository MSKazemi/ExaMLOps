import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8099',
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    // 79 jsdom files run in parallel, so a file's first test pays render + import cost while
    // workers contend. Measured: Config.test.tsx's first test takes ~4.4 s alone and timed out
    // at vitest's 5 s default inside the full suite — a false red on a suite that gates deploys.
    testTimeout: 15000,
    hookTimeout: 15000,
  },
})
