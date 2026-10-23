import { expect, test, type Page, type Route } from "@playwright/test";

import { TOUCH_FLOOR } from "./touchTarget";

// #1596 (epic #1531 Phase 12): every phase of the public share viewer in NIGHTJAR, in a real
// browser — loading, the password gate with its error line and the 429 wait, unavailable, relink,
// nocookie, offline with Try again, and the terminal note. The full flows (create, unlock, watch,
// send, revoke) are e2e/public-share.spec.ts; this pins each phase's look and its controls. The
// server is mocked per test at `/api/public/*`; `/p/<token>` serves the built share.html.

const TOKEN = "tok" + "x".repeat(40);
const CSRF = "visitor-csrf";
const share = (kind: "api" | "terminal") => ({
  link: "L1",
  kind,
  mode: "view",
  title: "Fix the auth redirect loop",
  expires_at: null,
  name: null,
  name_required: false,
});

type Handler = (path: string, route: Route) => Promise<void> | void;

async function openViewer(page: Page, path: string, handler: Handler): Promise<void> {
  await page.route(/\/p\/[A-Za-z0-9_-]*$/, async (r) => {
    const response = await r.fetch({ url: new URL("/share.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await page.route(/\/api\/public\/.*/, (r) => handler(new URL(r.request().url()).pathname, r));
  await page.goto(path);
}

/** The phase is a NIGHTJAR panel (chamfer + rail), its callsign carries the hazard notch, and
 *  nothing scrolls sideways. */
async function expectPanel(page: Page, testId: string): Promise<void> {
  const panel = page.getByTestId(testId);
  await expect(panel).toBeVisible();
  await expect(panel).toHaveClass(/\bnj-frame\b/);
  await expect(panel).not.toHaveCSS("clip-path", "none");
  const notch = await panel.locator("p").first().evaluate((el) => getComputedStyle(el, "::before").backgroundImage);
  expect(notch).toContain("repeating-linear-gradient");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

test("loading: the radar and its words while the link opens", async ({ page }) => {
  await openViewer(page, `/p/${TOKEN}`, () => {}); // the unlock never answers
  await expect(page.getByTestId("share-viewer")).toHaveAttribute("data-phase", "loading");
  const status = page.getByRole("status").filter({ hasText: "Opening the shared session…" });
  await expect(status).toBeVisible();
  await expect(status.locator(".nj-radar")).toBeVisible();
  // The bar carries the lit rail.
  await expect(page.locator("header").first()).toHaveCSS("border-top-width", "2px");
});

test("password: the gate, a wrong password's error line, then the 15-minute wait", async ({ page }) => {
  let tries = 0;
  await openViewer(page, `/p/${TOKEN}`, (path, r) => {
    if (path !== "/api/public/unlock") return r.fulfill({ status: 401, json: { detail: "unlock first" } });
    const body = r.request().postDataJSON() as { password?: string };
    if (!body.password) return r.fulfill({ status: 401, json: { password_required: true } });
    tries += 1;
    if (tries === 1) return r.fulfill({ status: 401, json: { password_required: true, tries_left: 2 } });
    return r.fulfill({ status: 429, json: { detail: "too many tries" } });
  });
  const gate = page.getByTestId("share-password-gate");
  await expectPanel(page, "share-password-gate");
  const field = gate.getByLabel("Password");
  await expect(field).toHaveClass(/\bnj-field\b/);
  const open = gate.getByRole("button", { name: "Open session" });
  for (const b of [open, gate.getByRole("button", { name: "Show" })]) {
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }

  await field.fill("wrong one");
  await open.click();
  await expect(gate.getByRole("alert")).toHaveText("Wrong password. 2 tries left before a 15-minute pause.");
  await expect(gate.getByRole("alert")).toHaveCSS("border-left-width", "2px");
  await field.fill("wrong two");
  await open.click();
  await expect(gate.getByRole("alert")).toHaveText("Too many wrong passwords. Try again in 15 minutes.");
  // The pause is the server's (it answers 429 until it ends); the field stays usable for the retry.
  await expect(field).toBeEditable();
});

test("unavailable: a revoked, expired or wrong link", async ({ page }) => {
  await openViewer(page, `/p/${TOKEN}`, (_p, r) => r.fulfill({ status: 404, json: { detail: "link unavailable" } }));
  await expectPanel(page, "share-unavailable");
  await expect(page.getByTestId("share-unavailable")).toContainText("This link no longer works");
});

test("relink: a reload on /p/ without the cookie asks for the link again", async ({ page }) => {
  await openViewer(page, "/p/", (_p, r) => r.fulfill({ status: 401, json: { detail: "unlock this link first" } }));
  await expectPanel(page, "share-relink");
});

test("nocookie: a browser that drops the share cookie gets the explanation, not a loop", async ({ page }) => {
  await openViewer(page, `/p/${TOKEN}`, (path, r) => {
    if (path === "/api/public/unlock") return r.fulfill({ json: { share: share("api"), csrf: CSRF } });
    return r.fulfill({ status: 401, json: { detail: "unlock this link first" } });
  });
  await expectPanel(page, "share-nocookie");
});

test("offline, Try again, and the terminal note once the link opens", async ({ page }) => {
  let up = false;
  await openViewer(page, `/p/${TOKEN}`, (path, r) => {
    if (!up) return r.fulfill({ status: 503, json: { detail: "BattleLab is restarting" } });
    if (path === "/api/public/unlock") return r.fulfill({ json: { share: share("terminal"), csrf: CSRF } });
    return r.fulfill({ status: 404, json: {} });
  });
  await expectPanel(page, "share-offline");
  const again = page.getByRole("button", { name: "Try again" });
  expect((await again.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  up = true;
  await again.click();
  await expectPanel(page, "share-terminal-pending");
  await expect(page.getByTestId("share-terminal-pending")).toContainText("can’t be shown yet");
  // Open: the bar names the link and its mode.
  await expect(page.locator("header").first()).toContainText("Fix the auth redirect loop");
});

/** An open send link with one finished turn: what `routes/public_api.py` answers once unlocked. */
const SNAPSHOT = {
  link: "L1",
  revision: 3,
  event_cursor: 3,
  state: "idle",
  active_turn: null,
  model: "gpt-5-codex",
  turns: [
    {
      turn_id: "t1",
      state: "completed",
      delivery: null,
      text: "Login bounces between /login and / after the cookie refresh. Find out why.",
      text_truncated: false,
      reply: "The refresh middleware re-issues the cookie with `Path=/api`, so the SPA shell never sees it.",
      reply_ts: 1_800_000_000,
      reply_truncated: false,
      attachments: [],
      tools: [{ name: "read", outcome: "ok", summary: "src/auth/middleware.py · 212 lines" }],
      tools_truncated: false,
      author: null,
      mine: false,
    },
  ],
  omitted_turns: 0,
  pending_requests: [],
  read_only: false,
  images: true,
  can_send: true,
  mission_held: false,
  background: false,
};

test("the open conversation: the answer, its tools and the composer on the NIGHTJAR page", async ({ page }) => {
  await openViewer(page, `/p/${TOKEN}`, (path, r) => {
    if (path === "/api/public/unlock" || path === "/api/public/share")
      return r.fulfill({ json: { share: { ...share("api"), mode: "send", name: "Alex" }, csrf: CSRF } });
    if (path === "/api/public/session") return r.fulfill({ json: SNAPSHOT });
    if (path === "/api/public/events") return r.fulfill({ json: { link: "L1", revision: 3, next_cursor: 3, events: [] } });
    return r.fulfill({ status: 404, json: {} });
  });
  const transcript = page.getByTestId("share-transcript");
  await expect(transcript).toContainText("Find out why.");
  await expect(transcript).toContainText("Path=/api");
  await expect(transcript.getByTestId("share-activity")).toBeVisible();
  const composer = page.getByTestId("share-composer");
  await expect(composer).toBeVisible();
  // The bar carries the rail and the link's facts; the composer's field and Send are NIGHTJAR's.
  await expect(page.locator("header").first()).toHaveCSS("border-top-width", "2px");
  await expect(page.locator("header").first()).toContainText("Fix the auth redirect loop");
  await expect(composer.locator("textarea")).toHaveClass(/\bnj-field\b/);
  const send = composer.getByRole("button", { name: /send/i });
  await expect(send).toHaveClass(/\bnj-plate\b/);
  expect((await send.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await expect(page.locator(".nj-atmo")).toBeAttached();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

/** The viewer atmosphere's endless CSS animations that are running (the scan, the sweep). */
const runningLoops = (page: Page) =>
  page.getByTestId("nj-atmo").evaluate((el) =>
    el
      .getAnimations({ subtree: true })
      .filter((a) => a.effect?.getComputedTiming().iterations === Infinity && a.playState === "running").length,
  );

// #1601 review: the viewer has no stored motion setting, so it follows the device — LIVE, not
// just at load: turning reduced motion on stills the atmosphere, turning it off runs it again.
test("motion: the viewer follows a live reduced-motion change (#1596)", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await openViewer(page, `/p/${TOKEN}`, () => {}); // stays on loading; the atmosphere is behind it
  const html = page.locator("html");
  await expect(html).toHaveAttribute("data-motion", "full");
  await expect.poll(() => runningLoops(page)).toBeGreaterThan(0);

  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(html).toHaveAttribute("data-motion", "reduced");
  await expect(page.getByTestId("nj-atmo").locator(".nj-sweep")).toBeHidden();
  await expect.poll(() => runningLoops(page)).toBe(0);

  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(html).toHaveAttribute("data-motion", "full");
  await expect.poll(() => runningLoops(page)).toBeGreaterThan(0);
});

// #1601 review: the viewer owns the hidden-tab / idle pause, like the SPA's MotionProvider.
test("motion: a hidden tab and ten idle minutes pause the viewer's atmosphere (#1596)", async ({ page }) => {
  await page.clock.install();
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "dark" });
  await openViewer(page, `/p/${TOKEN}`, () => {});
  const html = page.locator("html");
  await expect(html).toHaveAttribute("data-motion", "full");
  await expect(html).not.toHaveAttribute("data-motion-paused", /.+/);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(html).toHaveAttribute("data-motion-paused", "hidden");
  await expect.poll(() => runningLoops(page)).toBe(0);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(html).not.toHaveAttribute("data-motion-paused", /.+/);

  await page.clock.fastForward("10:05");
  await expect(html).toHaveAttribute("data-motion-paused", "idle");
  await page.mouse.move(40, 40);
  await page.mouse.move(60, 60);
  await expect(html).not.toHaveAttribute("data-motion-paused", /.+/);
});
