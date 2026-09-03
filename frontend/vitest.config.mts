import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/*
 * `.mts` so the config is unambiguously ESM — Vite warns that loading ESM
 * syntax as CommonJS will stop working in a future major.
 *
 * `resolve.tsconfigPaths` is Vite's native replacement for the
 * `vite-tsconfig-paths` plugin, which Vite now reports as redundant.
 */
export default defineConfig({
  plugins: [react()],
  resolve: { tsconfigPaths: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    // Playwright specs live in tests/e2e and are driven by Playwright, not Vitest.
    include: ["tests/unit/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/types/**", "**/*.d.ts"],
    },
  },
});
