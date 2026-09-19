import { defineConfig } from "@playwright/test";

/**
 * The GATE 8 walkthrough, in a real browser against the real stack.
 *
 * Expects the API, a worker and the web app running (`make api`, `make worker`,
 * `make web`); the suite creates its own users and file and removes them after.
 * It drives the installed Chrome rather than a downloaded build, so it runs
 * behind a TLS-intercepting proxy with nothing to fetch.
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  // One workflow, in order: upload before review, approval before audit.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // A reconciliation run hashes the 50,000-provider snapshot over the network.
  timeout: 12 * 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "../reports/e2e/html" }]],
  outputDir: "../reports/e2e/results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    channel: process.env.E2E_CHANNEL ?? "chrome",
    // The checklist's floor: responsive down to ~1280px without breakage.
    viewport: { width: 1280, height: 800 },
    // The run is waited on with an explicit timeout; nothing else should take this long.
    actionTimeout: 30_000,
    navigationTimeout: 30_000,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
