import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 3311);
const BASE_URL = `http://127.0.0.1:${PORT}`;

/**
 * End-to-end and accessibility checks against a real build.
 *
 * The server runs `next start` over a production build rather than `next dev`:
 * the security headers, the error boundaries and the CSP all behave differently
 * in development, and those are exactly what these tests are for.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  ...(process.env.CI ? { workers: 1 } : {}),
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: `npm run build && npm run start -- --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
    env: {
      SYNAPSE_API_URL: process.env.SYNAPSE_API_URL ?? "http://127.0.0.1:8799",
      SYNAPSE_SERVICE_TOKEN: "e2e-service-token-0123456789abcdef",
      SYNAPSE_JWT_SECRET: "e2e-jwt-secret-0123456789abcdef01",
    },
  },
});
