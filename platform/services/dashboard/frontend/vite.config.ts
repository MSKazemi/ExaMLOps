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
    // These bound a *hang*, not a performance budget, and they have to clear the slowest honest
    // test by a wide margin because 79 jsdom files run in parallel and contend for cores.
    //
    // Measured on this suite (2026-08-23, full run, verbose reporter): the slowest test is
    // Config.test.tsx's `shows Save button for admin` at 7.6 s, then 6.0 / 5.7 / 4.7 / 4.6 s —
    // every one of them the *first* test in its file, which is where the file's render and import
    // cost lands. Where that time goes is not contention alone: one
    // `getByRole('button', { name })` scan on that page costs ~80 ms (~17 ms without the `name`,
    // so recomputing accessible names dominates), Testing Library re-runs it every 50 ms, and the
    // button needs ~1 s of query loading to appear — ~1.5 s for a single `findByRole`.
    //
    // 15 s was not enough: on 2026-08-23 that 7.6 s test timed out at 15 s inside `make check`,
    // then passed at 7.9 s of test time when re-run. That is a ~2x tail on a suite that gates
    // deploys, and the failure it produced said nothing about the code. 30 s keeps the tail clear
    // while still catching a genuine hang. Raise the *test*, not this number, if a test's honest
    // duration ever approaches half of it.
    testTimeout: 30000,
    hookTimeout: 30000,
  },
})
