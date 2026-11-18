import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    // Per-test limit: the only time-based block on a test. A test still running after
    // 10 s fails as timed out; nothing fails a test for merely being slow. Twice
    // Vitest's 5 s default, leaving room for slow jsdom/user-event tests; this
    // template's own slowest test runs in under 0.1 s. Hitting it means hung, not
    // "a bit slow". A test that truly needs longer passes its own timeout with a
    // comment saying why.
    testTimeout: 10_000,
    coverage: {
      provider: 'istanbul',
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/**/*.test.{ts,tsx}', 'src/test-setup.ts'],
    },
  },
});
