/** battlelabos.com in a real browser: the sub-pages in NIGHTJAR (#1597, epic #1531 Phase 12b), and the
 *  generated landing page + blog (#2280, scripts/build-landing).
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
  ".jpg": "image/jpeg",
  ".xml": "application/xml",
  ".md": "text/markdown; charset=utf-8",
};

/** Serve landing/ from disk; a missing file answers the site's own 404 page, as nginx will. */
async function serveLanding(page: Page) {
  await page.route(`${ORIGIN}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    // A directory URL (/, /blog/, /blog/tag/hq/) answers its index.html, as nginx does.
    const file = resolve(LANDING, path.endsWith("/") ? `${path.slice(1)}index.html` : path.slice(1));
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

// ---------------------------------------------------------------------------------------------
// The landing page and the blog (#2280). Generated by scripts/build-landing into landing/.

const SITE_PAGES = ["/", "/blog/", "/blog/tag/hq/", "/blog/first-mission/", "/blog/call-hq/"];

for (const path of SITE_PAGES) {
  test(`${path} fits the screen and its controls are real targets`, async ({ page }, info) => {
    await serveLanding(page);
    const res = await page.goto(`${ORIGIN}${path}`);
    expect(res?.status()).toBe(200);
    await expect(page.locator("main#main")).toBeVisible();
    const [scroll, client] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    expect(scroll, "no sideways scroll").toBeLessThanOrEqual(client + 1);
    if (info.project.name !== "mobile") return;
    const small = await page
      .locator(".top a, main a, main button, main summary, footer a")
      .evaluateAll((els, floor) =>
        els
          .filter((el) => el.getClientRects().length && getComputedStyle(el).display !== "none")
          // a link inside a paragraph is text, not a control; the floor is for controls
          .filter((el) => !el.closest("p, li:not(.cl), td, figcaption, .prose blockquote, .then"))
          .map((el) => [el.textContent?.trim().slice(0, 24) ?? "", el.getBoundingClientRect().height] as const)
          .filter(([, h]) => h < floor),
      TOUCH_FLOOR);
    expect(small, "every control is at least 44 px tall").toEqual([]);
  });
}

test("the landing page keeps every way to Connect", async ({ page }) => {
  await serveLanding(page);
  await page.goto(`${ORIGIN}/`);
  await expect(page.locator(".top a.hot[href='/connect']")).toBeVisible();
  await expect(page.locator(".hero .acts a[href='/connect']")).toHaveText(/Open connect page/);
  await expect(page.locator("#install .then a[href='/connect']")).toBeVisible();
  await expect(page.locator("#homefree + section a[href='/connect']")).toBeVisible();
  await expect(page.locator(".pricing a[href='/connect']")).toBeVisible();
});

test("the blog: search and the kind filter narrow the posts, and clearing brings them back", async ({ page }) => {
  await serveLanding(page);
  await page.goto(`${ORIGIN}/blog/`);
  const visible = () => page.locator(".fm-main .pcard:visible");
  const all = await visible().count();
  expect(all).toBeGreaterThan(1);
  await page.getByRole("searchbox", { name: "Search the blog" }).fill("secret");
  await expect(visible()).toHaveCount(1);
  await expect(visible().first()).toContainText("credential");
  await page.getByRole("searchbox", { name: "Search the blog" }).fill("");
  await page.getByRole("button", { name: "Field report" }).click();
  await expect(page.getByRole("button", { name: "Field report" })).toHaveAttribute("aria-pressed", "true");
  for (const kind of await visible().evaluateAll((els) => els.map((e) => e.getAttribute("data-kind"))))
    expect(kind).toBe("field-report");
  await page.getByRole("searchbox", { name: "Search the blog" }).fill("zzzz-no-such-post");
  await expect(page.locator("#empty")).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(visible()).toHaveCount(all);
});

test("a post: the embed runs sandboxed, the files download, and the code copies", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await serveLanding(page);
  await page.goto(`${ORIGIN}/blog/first-mission/`);
  const frame = page.frameLocator("iframe[title='Mission lifecycle']");
  await page.locator("iframe[title='Mission lifecycle']").scrollIntoViewIfNeeded();
  await expect(page.locator("iframe[title='Mission lifecycle']")).toHaveAttribute("sandbox", "allow-scripts");
  await frame.getByRole("button", { name: "review", exact: true }).click();
  await expect(frame.locator("#n")).toHaveText("review");
  const file = page.locator(".files a.file[download]").first();
  await expect(file).toHaveAttribute("href", "mission-brief.md");
  expect(await page.evaluate(async () => (await fetch("mission-brief.md")).ok), "the attached file is served").toBe(true);
  const code = page.locator(".code").first();
  await code.getByRole("button", { name: "Copy" }).click();
  await expect(code.getByRole("button", { name: "Copied" })).toBeVisible();
});

test("reduced motion: nothing on the landing page or the blog animates", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await serveLanding(page);
  for (const path of SITE_PAGES) {
    await page.goto(`${ORIGIN}${path}`);
    expect(await page.evaluate(() => document.getAnimations().length), path).toBe(0);
  }
});

// The output is relative throughout (/connect is the app, not this site), so it also renders under a
// path prefix: a BattleLab artifact preview serves it at /artifacts/v/<token>/. Served here at /pfx/.
test("under a path prefix, styles, fonts, media, navigation and downloads all resolve", async ({ page }) => {
  const missing: string[] = [];
  await page.route(`${ORIGIN}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/connect") return route.fulfill({ status: 200, body: "app", contentType: "text/plain" });
    // The brand kit pins the favicon link as root-absolute (brandAssets.test.ts); a browser asks for it.
    if (path === "/favicon.svg") return route.fulfill({ status: 200, body: readFileSync(resolve(LANDING, "favicon.svg")), contentType: TYPES[".svg"] });
    if (!path.startsWith("/pfx/")) {
      missing.push(path);
      return route.fulfill({ status: 404, body: "" });
    }
    const rel = path.slice("/pfx/".length);
    const file = resolve(LANDING, rel === "" || rel.endsWith("/") ? `${rel}index.html` : rel);
    if (file.startsWith(LANDING) && existsSync(file))
      return route.fulfill({ status: 200, body: readFileSync(file), contentType: TYPES[extname(file)] ?? "application/octet-stream" });
    missing.push(path);
    return route.fulfill({ status: 404, body: "" });
  });
  await page.route(/^https?:\/\/(?!battlelabos\.test)/, (route) => route.abort());
  await page.emulateMedia({ reducedMotion: "no-preference" });

  await page.goto(`${ORIGIN}/pfx/`);
  expect(await page.evaluate(() => getComputedStyle(document.querySelector(".h1")!).textTransform)).toBe("uppercase");
  expect(await page.evaluate(async () => {
    await document.fonts.ready;
    return [...document.fonts].some((f) => f.family.includes("Chakra") && f.status === "loaded");
  }), "the display font loads").toBe(true);
  await page.locator("#briefing").scrollIntoViewIfNeeded();

  await page.locator(".top nav").getByRole("link", { name: "Blog" }).click();
  await expect(page).toHaveURL(`${ORIGIN}/pfx/blog/`);
  await page.locator(".tagcloud a").first().click();
  await expect(page).toHaveURL(new RegExp(`^${ORIGIN}/pfx/blog/tag/[a-z0-9-]+/$`));
  await page.locator(".pcard h3 a").first().click();
  await expect(page).toHaveURL(new RegExp(`^${ORIGIN}/pfx/blog/[a-z0-9-]+/$`));
  await page.goto(`${ORIGIN}/pfx/blog/first-mission/`);
  expect(await page.evaluate(async () => (await fetch("mission-brief.md")).ok)).toBe(true);
  await page.locator(".crumbs").getByRole("link", { name: "Blog" }).click();
  await expect(page).toHaveURL(`${ORIGIN}/pfx/blog/`);

  expect(missing, "nothing is requested outside the prefix").toEqual([]);
});
