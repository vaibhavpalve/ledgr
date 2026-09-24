import { defineConfig, devices } from "@playwright/test";

/**
 * The golden path in a real browser (founder review 2026-09-24, item 8).
 *
 * Runs against a live stack - the web dev server proxying to a running API - because the class of
 * defect it exists to catch only appears there: a dev server that renders nothing while every unit
 * test passes (the StrictMode session trap), a posting path no screen reaches, a configuration
 * nobody seeds. `E2E_BASE_URL` is the web app; `E2E_API_URL` is the API the spec reads the dev
 * outbox from (EXPOSE_DEV_OUTBOX must be true there). Both default to the local stack.
 *
 *     corepack pnpm --filter @ledgr/web run test:e2e
 *
 * Screenshots of every screen the journey passes through land in `e2e/screenshots/`, for a person
 * to look at: an assertion says the button exists, not that the page looks right.
 */
export default defineConfig({
  testDir: "e2e",
  outputDir: "e2e/.results",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "nl-NL",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
