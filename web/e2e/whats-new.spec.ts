import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

/** What's new (#971) — the once-per-operator slideshow, in a real browser (desktop + mobile).
 *
 * The preview build is unstamped, and an unstamped bundle never auto-shows the dialog, so each test
 * states the stamp it stands in for on `window.__BATTLELAB_E2E_BUNDLE_VERSION__` — which the app
 * reads only when the build is unstamped (`whatsNewBundleVersion`). The server is mocked:
 * `/api/config` carries `whats_new_seen` (or omits it), `/api/version` answers (or fails), and
 * `/api/prefs` records every write and can be told to fail. The server's never-lower rule and its
 * onboarding fence are pinned in pytest (`tests/test_whats_new_seen.py`), not re-modelled here. */

// No-op WebSocket so the shell mounts without a backend (E2E serves the static SPA only).
const NOOP_WS = `
window.WebSocket = class {
  constructor() { this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 20); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

type Seen = string | null | "absent";

interface Scenario {
  /** The stamp this test stands in for; `null` leaves the build unstamped. Default `0.20.0`. */
  bundle?: string | null;
  /** `/api/version`; `null` makes it fail. Default `0.20.0`. */
  server?: string | null;
  onboarded?: boolean;
  seen?: Seen;
  prefsFail?: boolean;
}

async function setup(page: Page, s: Scenario = {}) {
  const state = {
    seen: (s.seen === undefined ? null : s.seen) as Seen,
    onboarded: s.onboarded ?? true,
    /** What `/api/version` answers next; `null` makes it fail. A test may change it mid-run. */
    server: (s.server === undefined ? "0.20.0" : s.server) as string | null,
  };
  const writes: Record<string, unknown>[] = [];
  const bundle = s.bundle === undefined ? "0.20.0" : s.bundle;

  await page.addInitScript(NOOP_WS);
  if (bundle !== null) {
    await page.addInitScript((v) => {
      (window as unknown as { __BATTLELAB_E2E_BUNDLE_VERSION__: string }).__BATTLELAB_E2E_BUNDLE_VERSION__ = v;
    }, bundle);
  }
  // Catch-all first: Playwright consults the most recently registered route first.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [{ id: "claude", present: true, supports_new: true, bin: "/x/claude" }] } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route("**/api/version", (r) =>
    state.server === null
      ? r.fulfill({ status: 503, json: { detail: "unavailable" } })
      : r.fulfill({ json: { version: state.server } }),
  );
  await page.route("**/api/config", (r) => {
    const json: Record<string, unknown> = {
      csrf: "x",
      auth_mode: "none",
      terminal_backend: "ws",
      new_session_engines: ["claude"],
      onboarded: state.onboarded,
    };
    if (state.seen !== "absent") json.whats_new_seen = state.seen;
    return r.fulfill({ json });
  });
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: {} });
    const body = r.request().postDataJSON() as Record<string, unknown>;
    writes.push(body);
    if (s.prefsFail) return r.fulfill({ status: 500, json: { detail: "could not write prefs" } });
    if (body.onboarded === true) state.onboarded = true;
    if (typeof body.whats_new_seen === "string") state.seen = body.whats_new_seen;
    return r.fulfill({ json: body });
  });
  return { state, writes };
}

const whatsNew = (page: Page) => page.getByRole("dialog", { name: /what's new/i });

/** Load a page and wait until the gate has every input it reads — the config and the version
 *  answer (or its failure) — so an absent dialog means "decided not to show", not "not yet". */
async function gotoSettled(page: Page, path = "/") {
  const config = page.waitForResponse((r) => r.url().endsWith("/api/config"));
  const version = page.waitForResponse((r) => r.url().endsWith("/api/version"));
  await page.goto(path);
  await Promise.all([config, version]);
  await page.waitForTimeout(400);
}

/** Client-side navigation: a re-render of the shell with the config it already holds. */
async function navigateInApp(page: Page, path: string) {
  await page.evaluate((p) => {
    window.history.pushState({}, "", p);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

test("shows once on 0.20.0; Escape dismisses and records it; a reload does not show it again", async ({ page }) => {
  const { writes } = await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await expect(d).toBeVisible();
  await expect(d.getByText("1 / 7")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(d).toHaveCount(0);
  await expect.poll(() => writes).toEqual([{ onboarded: true, whats_new_seen: "0.20.0" }]);
  await gotoSettled(page);
  await expect(whatsNew(page)).toHaveCount(0);
  expect(writes).toHaveLength(1);
});

const GATED: [string, Scenario][] = [
  ["an unstamped bundle", { bundle: null }],
  ["a config without the key (a server from before #971)", { seen: "absent" }],
  ["a stale tab — bundle 0.19.2 against server 0.20.0", { bundle: "0.19.2" }],
  ["a server version that is not known", { server: null }],
  ["notes another device already dismissed", { seen: "0.20.0" }],
  ["a pre-release", { bundle: "0.20.0rc1", server: "0.20.0rc1" }],
];
for (const [name, scenario] of GATED) {
  test(`does not auto-show for ${name}`, async ({ page }) => {
    const { writes } = await setup(page, scenario);
    await gotoSettled(page);
    await expect(whatsNew(page)).toHaveCount(0);
    expect(writes).toHaveLength(0);
  });
}

test("a fresh install gets the wizard, and finishing it records the notes so the dialog never follows", async ({ page }) => {
  const { writes } = await setup(page, { onboarded: false });
  await page.goto("/");
  const wizard = page.getByRole("dialog", { name: /set up battlelab/i });
  await expect(wizard).toBeVisible();
  await expect(whatsNew(page)).toHaveCount(0);
  await wizard.getByRole("button", { name: /skip setup/i }).click();
  await expect(wizard).toHaveCount(0);
  await expect.poll(() => writes).toEqual([{ onboarded: true, whats_new_seen: "0.20.0" }]);
  await expect(whatsNew(page)).toHaveCount(0);
  await gotoSettled(page);
  await expect(whatsNew(page)).toHaveCount(0);
});

test("a wizard save that fails closes the wizard without the dialog in that tab; the next load decides again", async ({ page }) => {
  const { writes, state } = await setup(page, { onboarded: false, prefsFail: true });
  await page.goto("/");
  const wizard = page.getByRole("dialog", { name: /set up battlelab/i });
  await wizard.getByRole("button", { name: /skip setup/i }).click();
  await expect(wizard).toHaveCount(0);
  await expect.poll(() => writes.length).toBe(1);
  await navigateInApp(page, "/overview");
  await navigateInApp(page, "/");
  await expect(whatsNew(page)).toHaveCount(0);
  // Nothing was recorded. If onboarding now resolves true anyway (a pref written by an earlier step),
  // the notes show on the next load — once.
  state.onboarded = true;
  await page.reload();
  await expect(whatsNew(page)).toBeVisible();
});

test("an open slideshow survives a version refresh: still on 2 / 7, nothing recorded (#977 review)", async ({ page }) => {
  const { state, writes } = await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await d.getByRole("button", { name: /show me/i }).click();
  await expect(d.getByText("2 / 7")).toBeVisible();
  // The server upgrades while the notes are open, and the app's own poll finds out: on the tab
  // coming back into view (useAppVersion's visibilitychange trigger). Nothing is due any more.
  state.server = "0.20.1";
  const polled = page.waitForResponse((r) => r.url().endsWith("/api/version"));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  const answer = await polled;
  expect(await answer.json()).toEqual({ version: "0.20.1" });
  await page.waitForTimeout(500);
  await expect(d).toBeVisible();
  await expect(d.getByText("2 / 7")).toBeVisible();
  expect(writes).toHaveLength(0);
  // It still closes the normal way, and that is what records it.
  await d.getByRole("button", { name: "Close what's new" }).click();
  await expect(d).toHaveCount(0);
  await expect.poll(() => writes).toEqual([{ onboarded: true, whats_new_seen: "0.20.0" }]);
});

test("✕ dismisses and records; a tap on the backdrop does neither", async ({ page }) => {
  const { writes } = await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await expect(d).toBeVisible();
  await page.mouse.click(4, 4);
  await expect(d).toBeVisible();
  expect(writes).toHaveLength(0);
  await d.getByRole("button", { name: "Close what's new" }).click();
  await expect(d).toHaveCount(0);
  await expect.poll(() => writes).toEqual([{ onboarded: true, whats_new_seen: "0.20.0" }]);
});

test("the last slide's button dismisses and records", async ({ page }) => {
  const { writes } = await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await d.getByRole("button", { name: /show me/i }).click();
  for (let i = 2; i < 7; i++) await d.getByRole("button", { name: /^next$/i }).click();
  await expect(d.getByText("7 / 7")).toBeVisible();
  await d.getByRole("button", { name: "Let's go" }).click();
  await expect(d).toHaveCount(0);
  await expect.poll(() => writes.length).toBe(1);
});

for (const [tile, cta, path] of [
  ["Mission control", "Open Missions", /\/mission$/],
  ["Templates", "Open Templates", /\/templates$/],
] as const) {
  test(`the ${cta} CTA navigates and records the dismissal`, async ({ page }) => {
    const { writes } = await setup(page);
    await page.goto("/");
    const d = whatsNew(page);
    await d.getByRole("button", { name: new RegExp(`^${tile}`, "i") }).click();
    await d.getByRole("button", { name: cta }).click();
    await expect(page).toHaveURL(path);
    await expect(d).toHaveCount(0);
    await expect.poll(() => writes).toEqual([{ onboarded: true, whats_new_seen: "0.20.0" }]);
  });
}

test("a save that fails keeps it closed in this tab — moving around does not reopen it — and a reload shows it again", async ({ page }) => {
  const { writes } = await setup(page, { prefsFail: true });
  await page.goto("/");
  const d = whatsNew(page);
  await d.getByRole("button", { name: "Close what's new" }).click();
  await expect(d).toHaveCount(0);
  await expect.poll(() => writes.length).toBe(1);
  await navigateInApp(page, "/overview");
  await navigateInApp(page, "/");
  await page.waitForTimeout(400);
  await expect(whatsNew(page)).toHaveCount(0);
  await page.reload();
  await expect(whatsNew(page)).toBeVisible();
});

test("reopening from Settings → About after a newer release was seen records nothing, and focus returns", async ({ page }) => {
  const { writes } = await setup(page, { bundle: "0.21.0", server: "0.21.0", seen: "0.21.0" });
  await gotoSettled(page, settingsPath("about"));
  await expect(whatsNew(page)).toHaveCount(0);
  const reopen = page.getByRole("button", { name: "What's new in 0.20" });
  await reopen.click();
  const d = whatsNew(page);
  await expect(d).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(d).toHaveCount(0);
  await expect(reopen).toBeFocused();
  await page.waitForTimeout(400);
  expect(writes).toHaveLength(0);
});

test("keyboard: the arrows move, Tab stays inside, and the page behind is inert", async ({ page }) => {
  await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await expect(d).toBeVisible();
  await expect(page.locator("#root")).toHaveAttribute("inert", "");
  await page.keyboard.press("ArrowRight");
  await expect(d.getByText("2 / 7")).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await expect(d.getByText("1 / 7")).toBeVisible();
  for (let i = 0; i < 14; i++) {
    await page.keyboard.press("Tab");
    const inside = await page.evaluate(
      () => !!document.activeElement?.closest("[data-testid='whats-new']"),
    );
    expect(inside, `Tab #${i + 1} left the dialog`).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(page.locator("#root")).not.toHaveAttribute("inert", "");
});

test("desktop: the dots jump to a slide", async ({ page }, info) => {
  test.skip(info.project.name === "mobile", "the dots are hidden at ≤800px, where the counter carries position");
  await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await d.getByRole("button", { name: "Slide 4 of 7" }).click();
  await expect(d.getByText("4 / 7")).toBeVisible();
  await expect(d.getByRole("heading", { name: /edit where you read/i })).toBeVisible();
});

test("phone: 44px targets, no horizontal overflow at 320px, and the footer reachable on every slide", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "phone layout");
  await page.setViewportSize({ width: 320, height: 640 });
  await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await expect(d.getByRole("group", { name: "Slides" })).toBeHidden();
  await d.getByRole("button", { name: /show me/i }).click();
  for (const name of ["Close what's new", "Pause animation", "Open Missions", "Back", "Next"]) {
    const box = await d.getByRole("button", { name, exact: true }).boundingBox();
    expect(box, name).not.toBeNull();
    expect(box!.height, name).toBeGreaterThanOrEqual(44);
  }
  for (let i = 2; i <= 7; i++) {
    expect(await d.evaluate((el) => el.scrollWidth - el.clientWidth), `slide ${i} overflows`).toBeLessThanOrEqual(0);
    const primary = d.getByRole("button", { name: /^(next|let's go)$/i });
    await expect(primary).toBeInViewport();
    if (i < 7) await primary.click();
  }
});

test("the illustration loads, and Pause swaps it to the still image", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await setup(page);
  await page.goto("/");
  const d = whatsNew(page);
  await d.getByRole("button", { name: /show me/i }).click();
  const img = d.locator("img");
  const loaded = () => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0);
  await expect(img).toHaveAttribute("src", /whatsnew\/0\.20\/missions\.svg$/);
  await expect.poll(loaded).toBe(true);
  const pause = d.getByRole("button", { name: "Pause animation" });
  await pause.click();
  await expect(pause).toHaveAttribute("aria-pressed", "true");
  await expect(img).toHaveAttribute("src", /missions-still\.svg$/);
  await expect.poll(loaded).toBe(true);
});
