/** The Home Free connect page with the REAL SPA mounted and a harness standing in for the tunnel
 *  (#1658), shared by the relay specs. The app's API is mocked at the network edge. */
import { expect, type Page } from "@playwright/test";

import { verifyHuman } from "./connect-helpers";

// The public deploy's host (it defaults the relay and canonicalizes to /connect/), served by the
// preview — the same mapping homefree-connect.spec.ts uses.
export const RELAY_HOST_LAUNCH = {
  launchOptions: {
    args: ["--host-resolver-rules=MAP battlelab.superstatus.io 127.0.0.1"],
    // Replaces the config's launchOptions, so repeat its shared-memory choice (playwright.config.ts).
    ignoreDefaultArgs: ["--disable-dev-shm-usage"],
  },
};

const ZERO_ALTCHA = "5feceb66ffc86f38d952786c6d696c79c2dbc239dd4e91b46729d73a27fb57e9";

export function connectUrl(baseURL: string | undefined): string {
  const u = new URL(baseURL ?? "http://localhost:41873");
  return `http://battlelab.superstatus.io:${u.port}/connect.html`;
}

export async function mockApi(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  await page.route("https://relay.battlelab.superstatus.io/altcha/challenge", (r) =>
    r.fulfill({
      json: { algorithm: "SHA-256", challenge: ZERO_ALTCHA, salt: "", signature: "t", maxnumber: 0 },
    }),
  );
}

/** Connect with the real SPA mounted. `warnEarly` fires the relay's `warn` while the app is still
 *  mounting — before the shell is connected and before anything is published. */
export async function connectRelayed(
  page: Page,
  baseURL: string | undefined,
  opts: {
    warnEarly?: boolean;
    /** Extra API mocks, registered after the defaults so they win. */
    api?: (page: Page) => Promise<void>;
    /** Stop after pressing Connect: for an attempt the box ends during the app's mount. */
    expectConnected?: boolean;
  } = {},
) {
  await mockApi(page);
  if (opts.api) await opts.api(page);
  await page.addInitScript(({ warnEarly }) => {
    type Evt = { type: string; deadline?: number; ttl?: number; remaining?: number };
    window.__battlelabConnectHarness = {
      holdMs: 250,
      makeWebSocket: () => ({
        binaryType: "arraybuffer",
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
        readyState: 1,
        send() {},
        close() {},
      }),
      mountApp: async (_ws, _key, _captcha, opts) => {
        const fire = (e: Evt) => opts?.onEvent?.(e as never);
        (window as unknown as { __relayFire: (e: Evt) => void }).__relayFire = fire;
        fire({ type: "paired", deadline: Math.floor(Date.now() / 1000) + 14400, ttl: 14400 });
        if (warnEarly) fire({ type: "warn", remaining: 240 });
        // The page hands over the real SPA render (#1658). A page that does not (a build without
        // it) still connects, with a placeholder app — so what fails is the plate, not the mount.
        const root = document.getElementById("app-root")!;
        const render = opts?.render
          ? await opts.render("app-root")
          : ((root.textContent = "app without a render seam"), () => (root.textContent = ""));
        let gone = false;
        const unmount = () => {
          if (!gone) render();
          gone = true;
        };
        const w = window as unknown as { __unmountApp: () => void; __holdMount?: Promise<void> };
        w.__unmountApp = unmount; // the app going away while the shell is still connected
        if (w.__holdMount) await w.__holdMount; // the test keeps the attempt in its mount
        return { teardown: unmount };
      },
    };
  }, { warnEarly: opts.warnEarly ?? false });
  await page.goto(connectUrl(baseURL));
  await page.getByLabel("Console key").fill("onyx-9780");
  await page.getByLabel("Access password").fill("stream-secret");
  await verifyHuman(page);
  await page.getByRole("button", { name: "Connect" }).click();
  if (opts.expectConnected === false) return;
  await expect(page.locator("body")).toHaveAttribute("data-state", "connected");
  await expect(page.getByTestId("relay-plate")).toBeVisible({ timeout: 20_000 });
}

export const fire = (page: Page, e: { type: string; remaining?: number }) =>
  page.evaluate((evt) => (window as unknown as { __relayFire: (x: unknown) => void }).__relayFire(evt), e);
