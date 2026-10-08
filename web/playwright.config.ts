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
// holds 4173 permanently → `--strictPort` collided and failed every e2e run. The shared runner
// also runs MULTIPLE agent-sessions web-ci jobs concurrently (several open PRs), so a single fixed
// 41873 collides too ("port already used"). Derive a per-run port from the CI run id (falling back
// to the pid locally) so concurrent jobs never clash; still override-able via E2E_PORT.
// A CI run id is stable across Playwright's main + worker processes (it's an inherited env var),
// so it yields ONE port per run that every process agrees on — `process.pid` would differ per
// worker and break the webServer/baseURL match. Local (no run id, single dev, no concurrency)
// keeps the fixed 41873. web-ci shards the suite (#1099) and every shard shares one run id, so
// the workflow sets E2E_PORT per shard from a disjoint 46000+ range.
const CI_RUN = process.env.GITHUB_RUN_ID ?? process.env.GITHUB_RUN_NUMBER;
const PORT = Number(
  process.env.E2E_PORT ?? (CI_RUN ? 41000 + ((Number(CI_RUN) % 4000) + 1) : 41873),
);
// 127.0.0.1, not localhost: hosts that resolve localhost to ::1 first (the runner) bind the
// preview server IPv6-only, and the homefree specs' `MAP battlelab.superstatus.io 127.0.0.1`
// resolver rule then gets ERR_CONNECTION_REFUSED.
const PREVIEW_URL = `http://127.0.0.1:${PORT}`;

// The preview server must die with this Playwright process, however it dies. Playwright runs the
// webServer in its own process group, so a shard stopped by sibling-watch (#1244) or a runner
// cancel — which signal the step's group, or SIGKILL Playwright before its teardown runs — left
// `vite preview` running, reparented to init and squatting its port (15 orphans, up to 2h old,
// reaped from the runner on 2026-10-07). In CI the command execs vite directly (no npm/sh layer
// in between) under `setpriv --pdeathsig KILL`, so the KERNEL kills it the moment its parent
// Playwright exits. Local runs keep `npm run preview` (setpriv is Linux-only).
const PREVIEW_CMD = process.env.CI
  ? `exec setpriv --pdeathsig KILL -- node ./node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port ${PORT} --strictPort`
  : `npm run preview -- --host 127.0.0.1 --port ${PORT} --strictPort`;

// Playwright launches Chromium with --disable-dev-shm-usage by default, which moves its shared
// memory into the temp dir. That flag is for containers with a tiny /dev/shm; on the CI host
// executor /dev/shm is a large tmpfs, and the temp dir sat on the runner's disk — the measured CI
// bottleneck (a shared HDD mirror). Keep Chromium's shared memory in /dev/shm. Specs that set
// their own `launchOptions` (homefree-*) must repeat this, since `test.use` replaces the object.
const CHROMIUM_LAUNCH = { ignoreDefaultArgs: ["--disable-dev-shm-usage"] };

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  // Cap CI concurrency: every worker loads the SPA (xterm + WS) against the single shared
  // `vite preview` server, so the default (~half the runner's cores) saturates page setup and
  // specs flake with "Test timeout … setting up page". 2 keeps it parallel but reliable; local
  // stays uncapped for speed. CI scales OUT instead (#1099): web-ci runs 4 shards, each with its
  // own preview server at this same 2-worker load.
  workers: process.env.CI ? 2 : undefined,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  // Even with the CI worker cap, the shared org runner is CPU-starved enough that a web-first /
  // `expect.poll` assertion can briefly exceed the 5s default and flake a PASSING test — observed:
  // compose-draft.spec.ts's debounced server-side draft-clear timing out at 5s in CI while it
  // clears in ~2s locally (passes 9/9). Give assertions headroom so load — not correctness — never
  // reds web-ci.
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? PREVIEW_URL,
    // A session URL opened from outside the app asks "Full screen or map?" on desktop (#1232).
    // Every spec navigates straight to session URLs, so the suite starts with the choice made —
    // full screen, which is what those specs were written against. `link-open.spec.ts` clears it.
    storageState: {
      cookies: [],
      origins: [
        {
          origin: new URL(process.env.E2E_BASE_URL ?? PREVIEW_URL).origin,
          localStorage: [{ name: "battlelab.linkOpenMode", value: "fullscreen" }],
        },
      ],
    },
    launchOptions: CHROMIUM_LAUNCH,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  // No webServer when E2E_BASE_URL is set (point at a real app); otherwise preview the build.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: PREVIEW_CMD,
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
