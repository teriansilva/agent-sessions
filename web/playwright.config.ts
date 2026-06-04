import { defineConfig, devices } from "@playwright/test";

/** E2E + mobile harness (#64). The whole point: prove touch scroll, reconnect-
 *  without-blank, and responsive layout on a REAL emulated phone (hasTouch) — so
 *  terminal/UI changes are verified, never iterated blind. CI installs browsers
 *  (`npx playwright install --with-deps chromium`) before running.
 *
 *  Serves the built SPA via `vite preview`; backend-dependent specs (live session,
 *  reconnect) point at a running app instance and are tagged so they can be skipped
 *  where no backend is available. */
// Preview port. NOT vite's default 4173: the self-hosted CI runner is shared, and another project
// holds 4173 permanently → `--strictPort` collided and failed every e2e run. Use a distinct,
// override-able port so agent-sessions' preview never clashes with a neighbour on the same host.
const PORT = Number(process.env.E2E_PORT ?? 41873);
const PREVIEW_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? PREVIEW_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  // No webServer when E2E_BASE_URL is set (point at a real app); otherwise preview the build.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run preview -- --port ${PORT} --strictPort`,
        url: PREVIEW_URL,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    // The reason this harness exists: a real touch device.
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
});
