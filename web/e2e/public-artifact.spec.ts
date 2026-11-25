import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { expect, test, type Browser, type TestInfo } from "@playwright/test";
import { TOUCH_FLOOR } from "./touchTarget";

// #2068: a publicly shared artifact opens under the BattleLab header, in a real browser.
//
// The visitor is a FRESH context with no account, holding a BattleLab cookie it must never leak to
// the artifact. It is served the built `artifact.html` at /artifacts/p/<id>/ (the server's
// `public_viewer`), the link's `~meta`, and the artifact's own page at `~/`. That page is served
// here WITHOUT the server's `CSP: sandbox` header on purpose: the frame's own `sandbox` attribute
// is what this spec proves holds (the header is the other lock, pinned by tests/test_artifacts.py
// and e2e/appmode-artifacts.spec.ts). The visitor's BattleLab cookie must never reach the page's
// script or ride along on its requests.

const ID = "Ar7fQ2xAAAAAAAAAAAAAAA";
const P = `/artifacts/p/${ID}/`;
const SITE = "https://battlelabos.com/";
const SECRET = "operator-secret-value";

const PROBE = `<!doctype html><html><head><meta charset="utf-8"><title>Probe</title></head>
<body style="margin:0;padding:24px;font:16px system-ui"><h1 id="h">Q4 latency report</h1>
<script>
const b = document.body.dataset;
try { b.cookie = "read:" + document.cookie; } catch (e) { b.cookie = "blocked"; }
try { localStorage.setItem("x", "1"); b.storage = "read"; } catch (e) { b.storage = "blocked"; }
try { b.parent = "read:" + window.parent.document.title; } catch (e) { b.parent = "blocked"; }
fetch("/api/artifact-probe", { credentials: "include" })
  .then((r) => r.text())
  .then((t) => { b.api = "read:" + t.slice(0, 60); })
  .catch(() => { b.api = "blocked"; });
</script></body></html>`;

async function visitor(
  browser: Browser,
  testInfo: TestInfo,
  meta: "live" | "gone" = "live",
  content: string = PROBE,
) {
  const use = testInfo.project.use;
  const ctx = await browser.newContext({
    viewport: use.viewport,
    userAgent: use.userAgent,
    deviceScaleFactor: use.deviceScaleFactor,
    isMobile: use.isMobile,
    hasTouch: use.hasTouch,
    baseURL: use.baseURL,
  });
  await ctx.addCookies([{ name: "as_session", value: SECRET, url: use.baseURL!, sameSite: "Lax" }]);
  const apiCookies: string[] = [];
  const siteRequests: { referer: string | undefined }[] = [];
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "a route the viewer must not call" } }));
  // The framed page's probe goes to the REAL preview server, not a Playwright fulfil: a fulfilled
  // answer skips the browser's CORS check, which is exactly what keeps an opaque origin from
  // reading a same-host response. What matters here is the cookie it carried.
  await ctx.route("**/api/artifact-probe", (r) => {
    apiCookies.push(r.request().headers()["cookie"] ?? "");
    return r.continue();
  });
  await ctx.route((u) => u.pathname === P, async (r) => {
    const response = await r.fetch({ url: new URL("/artifact.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await ctx.route((u) => u.pathname === `${P}~meta`, (r) =>
    meta === "live"
      ? r.fulfill({ json: { title: "Q4 latency report", kind: "site", updated_at: Date.now() / 1000 - 7200 } })
      : r.fulfill({ status: 404, json: { detail: "Not Found" } }),
  );
  await ctx.route((u) => u.pathname === `${P}~/`, (r) =>
    r.fulfill({ status: 200, contentType: "text/html", body: content }),
  );
  await ctx.route(`${SITE}**`, (r) => {
    siteRequests.push({ referer: r.request().headers()["referer"] });
    return r.fulfill({ status: 200, contentType: "text/html", body: "<title>BattleLab OS</title>" });
  });
  return { ctx, page: await ctx.newPage(), apiCookies, siteRequests };
}

test("the artifact opens under the BattleLab header; the logo goes to battlelabos.com", async ({ browser }, testInfo) => {
  const { ctx, page, siteRequests } = await visitor(browser, testInfo);
  await page.goto(P);
  const bar = page.getByTestId("public-bar");
  await expect(bar).toHaveAttribute("data-collapsed", "false");
  await expect(bar).toContainText("Q4 latency report");
  await expect(bar).toContainText("SITE");
  await expect(page).toHaveTitle("Q4 latency report · BattleLab");
  await expect(page.frameLocator("[data-testid=artifact-frame]").locator("#h")).toHaveText("Q4 latency report");

  const brand = page.getByTestId("public-bar-brand");
  await expect(brand).toHaveAttribute("href", SITE);
  await expect(brand).toHaveAttribute("target", "_blank");
  for (const control of [brand, page.getByTestId("public-bar-toggle")]) {
    const box = (await control.boundingBox())!;
    if (testInfo.project.name === "mobile") {
      expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }

  // A new tab on battlelabos.com, with no handle back on this one and no Referer (the address of
  // a public page can carry a token).
  const [site] = await Promise.all([ctx.waitForEvent("page"), brand.click()]);
  await site.waitForLoadState();
  expect(site.url()).toBe(SITE);
  expect(await site.evaluate(() => window.opener)).toBeNull();
  expect(siteRequests[0]?.referer).toBeUndefined();
  // The share stays open where it was.
  expect(new URL(page.url()).pathname).toBe(P);
  await ctx.close();
});

test("collapsing gives the artifact the whole viewport, and stays collapsed for the visit", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo);
  await page.goto(P);
  const frame = page.getByTestId("artifact-frame");
  await expect(page.frameLocator("[data-testid=artifact-frame]").locator("#h")).toBeVisible();
  const open = (await frame.boundingBox())!;
  expect(open.y).toBeGreaterThan(40);

  await page.getByRole("button", { name: "Collapse the header" }).click();
  const bar = page.getByTestId("public-bar");
  await expect(bar).toHaveAttribute("data-collapsed", "true");
  await expect(bar).not.toContainText("Q4 latency report");
  // The frame now starts at the top and fills the viewport; the tab floats over its corner.
  await expect.poll(async () => (await frame.boundingBox())!.y).toBe(0);
  const vp = page.viewportSize()!;
  const full = (await frame.boundingBox())!;
  expect(Math.round(full.height)).toBe(vp.height);
  const tab = (await bar.boundingBox())!;
  expect(tab.x + tab.width).toBeLessThanOrEqual(vp.width);
  expect(tab.width).toBeLessThan(vp.width / 2);
  // The mark still links out from the tab.
  await expect(page.getByTestId("public-bar-brand")).toHaveAttribute("href", SITE);

  await page.reload();
  await expect(page.getByTestId("public-bar")).toHaveAttribute("data-collapsed", "true");
  await page.getByRole("button", { name: "Show the header" }).click();
  await expect(page.getByTestId("public-bar")).toContainText("Q4 latency report");
  await ctx.close();
});

test("the framed artifact can't read the cookie, storage, the page above it, or /api", async ({ browser }, testInfo) => {
  const { ctx, page, apiCookies } = await visitor(browser, testInfo);
  await page.goto(P);
  const body = page.frameLocator("[data-testid=artifact-frame]").locator("body");
  await expect(body).toHaveAttribute("data-cookie", "blocked");
  await expect(body).toHaveAttribute("data-storage", "blocked");
  await expect(body).toHaveAttribute("data-parent", "blocked");
  await expect(body).toHaveAttribute("data-api", "blocked", { timeout: 15_000 });
  // The probe did reach the server, without the visitor's BattleLab cookie.
  expect(apiCookies.length).toBeGreaterThan(0);
  expect(apiCookies.filter((c) => c.includes(SECRET))).toEqual([]);
  await ctx.close();
});

test("a revoked link: the header, 'no longer works', and nothing framed", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo, "gone");
  await page.goto(P);
  await expect(page.getByTestId("artifact-unavailable")).toContainText("This link no longer works");
  await expect(page.getByTestId("public-bar-brand")).toHaveAttribute("href", SITE);
  await expect(page.getByTestId("artifact-frame")).toHaveCount(0);
  await ctx.close();
});

test("the shared session and shared reply pages wear the same header", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo);
  await ctx.route(/\/p\/tok[a-z]*$/, async (r) => {
    await r.fulfill({ response: await r.fetch({ url: new URL("/share.html", r.request().url()).toString() }) });
  });
  await ctx.route(/\/r\/[A-Za-z0-9_-]+$/, async (r) => {
    await r.fulfill({ response: await r.fetch({ url: new URL("/reply.html", r.request().url()).toString() }) });
  });
  for (const path of [`/p/tok${"x".repeat(40)}`, `/r/${ID}#k=AAAA`]) {
    await page.goto(path);
    const brand = page.getByTestId("public-bar-brand");
    await expect(brand, path).toHaveAttribute("href", SITE);
    await expect(brand, path).toHaveAttribute("target", "_blank");
    await expect(brand, path).toHaveAttribute("rel", /noopener/);
    await expect(page.getByTestId("public-bar-toggle"), path).toBeVisible();
  }
  await ctx.close();
});

// #2210: a Markdown artifact, as the server writes its page (`markdown_page`; the fixture is pinned
// to the server's own output by tests/test_artifacts.py). The page loads the REAL built renderer
// (`dist/assets/artifact-md.js`, one classic script) from inside the opaque frame: a module script
// would be a CORS request the frame can never satisfy, so this is the proof the bundle loads at all.
const MD_DIR = join(process.cwd(), "e2e", "fixtures", "artifact-md");
const MD_PAGE = readFileSync(join(MD_DIR, "report.html"), "utf8");
const MD_TEXT = readFileSync(join(MD_DIR, "report.md"), "utf8");
const NIGHTJAR = resolve(process.cwd(), "..", "src", "agent_sessions", "static", "nightjar");

test("a Markdown artifact reads as a document in the frame: rendered, Source, Download", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo, "live", MD_PAGE);
  // The page's three subresources come through the route too. The frame here is a fulfilled
  // response, which has no IP address, so Chrome's Local Network Access check counts it as public
  // and refuses its requests to this loopback preview server. (On a real server the page and the
  // assets share one host; e2e/appmode-artifacts.spec.ts loads them from it.) The bytes are the REAL
  // build's, and a fulfilled response has no CORS headers either, so a module script would still
  // fail here: the classic script is what's proven.
  await ctx.route("**/static/nightjar/tokens.css", (r) =>
    r.fulfill({ contentType: "text/css", body: readFileSync(join(NIGHTJAR, "tokens.css"), "utf8") }),
  );
  await ctx.route("**/assets/artifact-md.*", (r) => {
    const file = new URL(r.request().url()).pathname.split("/").pop()!;
    return r.fulfill({
      contentType: file.endsWith(".js") ? "text/javascript" : "text/css",
      body: readFileSync(join(process.cwd(), "dist", "assets", file), "utf8"),
    });
  });
  const downloads: string[] = [];
  await ctx.route((u) => u.pathname === `${P}~/` && u.searchParams.get("download") === "1", (r) => {
    downloads.push(r.request().url());
    return r.fulfill({
      status: 200,
      contentType: "text/plain; charset=utf-8",
      headers: { "Content-Disposition": 'attachment; filename="report.md"' },
      body: MD_TEXT,
    });
  });
  await page.goto(P);
  const frame = page.frameLocator("[data-testid=artifact-frame]");
  const doc = frame.locator("#md-doc");

  // Rendered by the real bundle: headings, the table, the task list, the code block.
  await expect(frame.locator("body")).toHaveAttribute("data-md", "rendered", { timeout: 15_000 });
  await expect(doc.getByRole("heading", { level: 1 })).toHaveText("Operating model — concept map");
  await expect(doc.getByRole("heading", { level: 2, name: "Roles" })).toBeVisible();
  await expect(doc.locator("table tbody tr")).toHaveCount(3);
  await expect(doc.locator("input[type=checkbox]")).toHaveCount(2);
  await expect(doc.locator("[data-md-code=yaml]")).toBeVisible();
  await expect(frame.locator("#md-source")).toBeHidden();
  // The sandbox holds: an opaque origin, the raw HTML never ran, the remote image never loaded.
  const inner = page.frames().find((f) => f !== page.mainFrame())!;
  expect(await inner.evaluate(() => self.origin)).toBe("null");
  await expect(frame.locator("body")).not.toHaveAttribute("data-injected", "1");
  await expect(doc).toContainText('<script>document.body.dataset.injected = "1"</script>');
  await expect(doc.locator("[data-md-remote-image=\"tracker.example\"]")).toBeVisible();
  expect(await inner.evaluate(() => [...document.images].map((i) => i.src))).toEqual([]);
  // External links leave in a new tab with no opener and no referrer.
  const ext = doc.getByRole("link", { name: "the source" });
  await expect(ext).toHaveAttribute("target", "_blank");
  await expect(ext).toHaveAttribute("rel", "noopener noreferrer");

  // No sideways scroll: the page fits; the wide table and the long code line scroll in their box.
  const fits = await inner.evaluate(() => {
    const root = document.scrollingElement!;
    const wrap = document.querySelector("#md-doc table")!.parentElement!;
    const cell = [...document.querySelectorAll("#md-doc td")].find((td) => td.textContent === "Operator")!;
    return {
      page: root.scrollWidth <= root.clientWidth,
      wrap: getComputedStyle(wrap).overflowX,
      scrolls: wrap.scrollWidth > wrap.clientWidth,
      // A word stays whole: its text is one line box, not "Op / era / tor".
      wordWhole: (() => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        return range.getClientRects().length === 1;
      })(),
    };
  });
  expect(fits.page).toBe(true);
  expect(fits.wrap).toBe("auto");
  expect(fits.wordWhole).toBe(true);
  // The wide table is wider than a phone: it scrolls inside its own box, the page doesn't.
  if (testInfo.project.name === "mobile") expect(fits.scrolls).toBe(true);

  // The controls hold the 44 px floor on a phone.
  const controls = [
    frame.getByRole("button", { name: "Rendered" }),
    frame.getByRole("button", { name: "Source" }),
    frame.getByRole("link", { name: /Download/ }),
  ];
  for (const c of controls) {
    await expect(c).toBeVisible();
    if (testInfo.project.name === "mobile") {
      const box = (await c.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }

  // Source: the stored text, exactly; Rendered brings the document back.
  await frame.getByRole("button", { name: "Source" }).click();
  await expect(frame.locator("body")).toHaveAttribute("data-md", "source");
  await expect(frame.locator("#md-source")).toBeVisible();
  expect(await frame.locator("#md-source").textContent()).toBe(MD_TEXT);
  await expect(doc).toBeHidden();
  expect(await inner.evaluate(() => document.scrollingElement!.scrollWidth <= document.scrollingElement!.clientWidth)).toBe(true);
  await frame.getByRole("button", { name: "Rendered" }).click();
  await expect(doc.getByRole("heading", { level: 1 })).toBeVisible();

  // Download: the same path with ?download=1, answered as an attachment.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    frame.getByRole("link", { name: /Download/ }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("report.md");
  expect(downloads).toHaveLength(1);
  await ctx.close();
});
