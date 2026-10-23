/** battlelabos.com's sub-pages in NIGHTJAR (#1597, epic #1531 Phase 12b), in a real browser.
 *
 *  The pages are static files in landing/; this spec serves them from disk on a test origin, so it
 *  runs in the existing web e2e jobs without a server of its own.
 *  - consent.js behaves exactly as before the restyle: switching analytics off stores both keys,
 *    switching on removes them, Global Privacy Control locks the control off, and without JS the
 *    settings say so. The state LED reads the state consent.js marks — it never decides it.
 *  - Every sub-page fits a phone (no sideways scroll) and its controls are real touch targets.
 *  - Reduced motion: nothing animates (the atmosphere and the cursor are the only motion). */
import { readFileSync, existsSync, mkdirSync } from "node:fs";
import { resolve, extname } from "node:path";
import { expect, test, type Page } from "@playwright/test";

import { TOUCH_FLOOR } from "./touchTarget";

const ORIGIN = "https://battlelabos.test";
const LANDING = resolve(process.cwd(), "../landing");
const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".png": "image/png",
  ".webp": "image/webp",
};

/** Serve landing/ from disk; a missing file answers the site's own 404 page, as nginx will. */
async function serveLanding(page: Page) {
  await page.route(`${ORIGIN}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    const file = resolve(LANDING, path === "/" ? "index.html" : path.slice(1));
    if (file.startsWith(LANDING) && existsSync(file) && !file.endsWith("/"))
      return route.fulfill({ status: 200, body: readFileSync(file), contentType: TYPES[extname(file)] ?? "application/octet-stream" });
    return route.fulfill({ status: 404, body: readFileSync(resolve(LANDING, "404.html")), contentType: TYPES[".html"] });
  });
  // Nothing leaves the test: the privacy page loads no tracker, and the docs/GitHub links are never followed.
  await page.route(/^https?:\/\/(?!battlelabos\.test)/, (route) => route.abort());
}

const keys = (page: Page) =>
  page.evaluate(() => ({ consent: localStorage.getItem("battlelab.consent"), off: localStorage.getItem("umami.disabled") }));

test.describe("the privacy page's analytics control (consent.js unchanged)", () => {
  test("off stores both keys, on removes them, and the LED reads the state", async ({ page }) => {
    await serveLanding(page);
    await page.goto(`${ORIGIN}/privacy.html#settings`);
    const box = page.locator("#consent-settings");
    const on = box.getByRole("button", { name: "Analytics on" });
    const off = box.getByRole("button", { name: "Analytics off" });
    await expect(box.locator("[data-consent-state]")).toHaveText("Analytics on.");
    await expect(on).toHaveAttribute("aria-pressed", "true");
    const led = () => box.locator("[data-consent-state]").evaluate((el) => getComputedStyle(el, "::before").backgroundColor);
    const lit = await led();

    await off.click();
    await expect(off).toHaveAttribute("aria-pressed", "true");
    await expect(box.locator("[data-consent-state]")).toHaveText("Analytics off — this browser is not tracked.");
    expect(await keys(page)).toEqual({ consent: "denied", off: "1" });
    expect(await led(), "the LED goes dark when analytics are off").not.toBe(lit);

    await on.click();
    await expect(on).toHaveAttribute("aria-pressed", "true");
    expect(await keys(page)).toEqual({ consent: null, off: null });
    expect(await led()).toBe(lit);
  });

  // Global Privacy Control locks TRACKING off, not the buttons (#1597, Hermes 102852): consent.js is
  // unchanged, so a click still records the choice — "on" clears a stored opt-out — while the
  // effective state, the pressed button and the LED stay off for as long as the browser sends GPC.
  test("Global Privacy Control keeps tracking off whatever is clicked", async ({ page }) => {
    await page.addInitScript(() => Object.defineProperty(navigator, "globalPrivacyControl", { get: () => true }));
    await serveLanding(page);
    await page.goto(`${ORIGIN}/privacy.html#settings`);
    const box = page.locator("#consent-settings");
    const state = box.locator("[data-consent-state]");
    await expect(state).toContainText("Global Privacy Control");
    await expect(box.getByRole("button", { name: "Analytics off" })).toHaveAttribute("aria-pressed", "true");

    await box.getByRole("button", { name: "Analytics off" }).click();
    expect(await keys(page), "switching off is still remembered").toEqual({ consent: "denied", off: "1" });
    await box.getByRole("button", { name: "Analytics on" }).click();
    expect(await keys(page), "switching on clears the stored opt-out").toEqual({ consent: null, off: null });
    await expect(box.getByRole("button", { name: "Analytics off" }), "GPC wins: the effective state stays off").toHaveAttribute("aria-pressed", "true");
    await expect(state).toContainText("Global Privacy Control");
  });

  test("without JavaScript the settings say they need it", async ({ browser }) => {
    const ctx = await browser.newContext({ javaScriptEnabled: false });
    const page = await ctx.newPage();
    await serveLanding(page);
    await page.goto(`${ORIGIN}/privacy.html#settings`);
    await expect(page.locator("[data-consent-state]")).toContainText("Settings need JavaScript.");
    await ctx.close();
  });
});

for (const path of ["/legal.html", "/privacy.html", "/404.html", "/no-such-page"]) {
  test(`${path} fits the screen and its controls are real targets`, async ({ page }, info) => {
    await serveLanding(page);
    const res = await page.goto(`${ORIGIN}${path}`);
    if (path === "/no-such-page") {
      expect(res?.status()).toBe(404);
      await expect(page.getByRole("heading", { name: "Sector not found" })).toBeVisible();
    }
    await expect(page.locator(".sfoot")).toBeVisible();
    const [scroll, client] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    expect(scroll, "no sideways scroll").toBeLessThanOrEqual(client + 1);
    if (info.project.name !== "mobile") return;
    const small = await page
      .locator(".top a, .sfoot a, .doc .ghost, .lost a, #consent-settings button")
      .evaluateAll((els, floor) =>
        els
          .filter((el) => el.getClientRects().length && getComputedStyle(el).display !== "none")
          .map((el) => [el.textContent?.trim().slice(0, 24) ?? "", el.getBoundingClientRect().height] as const)
          .filter(([, h]) => h < floor),
      TOUCH_FLOOR);
    expect(small, "every control is at least 44 px tall").toEqual([]);
  });
}

test("reduced motion: nothing on the sub-pages animates", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await serveLanding(page);
  for (const path of ["/legal.html", "/privacy.html", "/404.html"]) {
    await page.goto(`${ORIGIN}${path}`);
    expect(await page.evaluate(() => document.getAnimations().length), path).toBe(0);
  }
});

// The COMPLETE set per page, not containment: a third animation (the 404 radar spun once,
// Hermes 6787) must fail here. The atmosphere and the cursor are the only motion.
for (const path of ["/legal.html", "/privacy.html", "/404.html"]) {
  test(`full motion on ${path}: exactly the atmosphere sweep and the cursor blink`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await serveLanding(page);
    await page.goto(`${ORIGIN}${path}`);
    const names = await page.evaluate(() =>
      document.getAnimations().map((a) => (a as CSSAnimation).animationName).sort(),
    );
    expect(names).toEqual(["lp-blink", "lp-sweep"]);
  });
}

/** The capture mode (as e2e/appmode-served-pages.spec.ts): with NIGHTJAR_CAPTURE_DIR set, each
 *  sub-page state is photographed at 1440 and 390, named `<capture id>-<width>-dark-1x.png` like
 *  the sweep (visual/nightjar); the share card at its own 1200 × 630. The ids are the ledger's
 *  `site:*` captures. The privacy control is shot in each state it can show — on, off, Global
 *  Privacy Control, no JavaScript — scrolled into view, and its state line is asserted first so a
 *  shot cannot show the wrong state. The public site is dark only, and one project is enough. */
const CAPTURE_DIR = process.env.NIGHTJAR_CAPTURE_DIR;
test("capture mode: the sub-pages as the ledger's site:* captures", async ({ browser }, info) => {
  test.skip(!CAPTURE_DIR || info.project.name !== "desktop", "only with NIGHTJAR_CAPTURE_DIR, desktop project");
  info.setTimeout(120_000);
  mkdirSync(CAPTURE_DIR!, { recursive: true });
  type Shot = {
    id: string;
    path: string;
    sizes?: [number, number][];
    js?: boolean;
    gpc?: boolean;
    act?: (page: Page) => Promise<void>;
  };
  const control = (state: string, click?: string) => async (page: Page) => {
    const box = page.locator("#consent-settings");
    if (click) await box.getByRole("button", { name: click }).click();
    await expect(box.locator("[data-consent-state]")).toContainText(state);
    await box.scrollIntoViewIfNeeded();
  };
  const shots: Shot[] = [
    { id: "site:legal", path: "/legal.html" },
    { id: "site:privacy", path: "/privacy.html" },
    { id: "site:privacy-on", path: "/privacy.html#settings", act: control("Analytics on.") },
    { id: "site:privacy-off", path: "/privacy.html#settings", act: control("Analytics off — this browser is not tracked.", "Analytics off") },
    { id: "site:privacy-gpc", path: "/privacy.html#settings", gpc: true, act: control("Global Privacy Control") },
    { id: "site:privacy-nojs", path: "/privacy.html#settings", js: false, act: control("Settings need JavaScript.") },
    { id: "site:404", path: "/no-such-page" },
    { id: "site:og", path: "/og.svg", sizes: [[1200, 630]] }, // a share card, not a page
  ];
  for (const shot of shots)
    for (const [w, h] of shot.sizes ?? [
      [1440, 960],
      [390, 844],
    ]) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, javaScriptEnabled: shot.js ?? true, reducedMotion: "reduce" });
      if (shot.gpc) await ctx.addInitScript(() => Object.defineProperty(navigator, "globalPrivacyControl", { get: () => true }));
      const page = await ctx.newPage();
      await serveLanding(page);
      await page.goto(`${ORIGIN}${shot.path}`);
      if (shot.act) await shot.act(page);
      await page.waitForTimeout(150);
      await page.screenshot({ path: resolve(CAPTURE_DIR!, `${shot.id.replace(/[:/]/g, "_")}-${w}-dark-1x.png`) });
      await ctx.close();
    }
});
