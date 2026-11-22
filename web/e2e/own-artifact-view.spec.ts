import { expect, test, type Browser, type TestInfo } from "@playwright/test";

// #2270: the operator's own artifact view wears the BattleLab header, in a real browser.
//
// Signed in, opening `/artifacts/<id>/` in a tab 303s to `/artifacts/o/<view token>/`: the built
// `artifact.html` in its own-view mode. Its one read is `~meta` under the same token (no cookie),
// and it frames the artifact from `/artifacts/v/<token>/`. The server half (the redirect only for
// a top-level page load, the token checks) is pinned by tests/test_artifacts.py; this spec proves
// the page, and the browser fact the redirect rests on: a tab says `Sec-Fetch-Dest: document`,
// while a frame and an <img> of the same link do not, so they keep getting the artifact itself.

const TOKEN = "eyJhIjoiNTQ0OS01NTkyLTIxMDctNDM3MiJ9.Zz-_9.s1g";
const O = `/artifacts/o/${TOKEN}/`;
const V = `/artifacts/v/${TOKEN}/`;

const PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>Report</title></head>
<body style="margin:0;padding:24px;font:16px system-ui"><h1 id="h">Weekly build report</h1></body></html>`;

async function operator(browser: Browser, testInfo: TestInfo, meta: "live" | "expired" = "live") {
  const use = testInfo.project.use;
  const ctx = await browser.newContext({
    viewport: use.viewport,
    userAgent: use.userAgent,
    deviceScaleFactor: use.deviceScaleFactor,
    isMobile: use.isMobile,
    hasTouch: use.hasTouch,
    baseURL: use.baseURL,
  });
  const metaCookies: string[] = [];
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "a route the viewer must not call" } }));
  await ctx.route((u) => u.pathname === O, async (r) => {
    const response = await r.fetch({ url: new URL("/artifact.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await ctx.route((u) => u.pathname === `${O}~meta`, async (r) => {
    metaCookies.push((await r.request().allHeaders())["cookie"] ?? "");
    return meta === "live"
      ? r.fulfill({
          // The server always answers header:true here; the page must not depend on it.
          json: { title: "Weekly build report", kind: "site", updated_at: Date.now() / 1000 - 3600, header: true },
        })
      : r.fulfill({ status: 404, contentType: "text/plain", body: "this view link has expired; open the artifact again\n" });
  });
  await ctx.route((u) => u.pathname === V, (r) => r.fulfill({ status: 200, contentType: "text/html", body: PAGE }));
  await ctx.addCookies([{ name: "as_session", value: "operator-cookie", url: use.baseURL!, sameSite: "Lax" }]);
  return { ctx, page: await ctx.newPage(), metaCookies };
}

test("Open lands on the artifact under the BattleLab header, which collapses", async ({ browser }, testInfo) => {
  const { ctx, page, metaCookies } = await operator(browser, testInfo);
  await page.goto(O);
  const bar = page.getByTestId("public-bar");
  await expect(bar).toHaveAttribute("data-collapsed", "false");
  await expect(bar).toContainText("Weekly build report");
  await expect(bar).toContainText("SITE");
  await expect(page).toHaveTitle("Weekly build report · BattleLab");
  await expect(page.getByTestId("artifact-frame")).toHaveAttribute("src", V);
  await expect(page.frameLocator("[data-testid=artifact-frame]").locator("#h")).toHaveText("Weekly build report");
  // The read is cookie-less: the view token in its path is the credential.
  expect(metaCookies).toEqual([""]);

  const frame = page.getByTestId("artifact-frame");
  expect((await frame.boundingBox())!.y).toBeGreaterThan(40);
  await page.getByRole("button", { name: "Collapse the header" }).click();
  await expect(bar).toHaveAttribute("data-collapsed", "true");
  await expect.poll(async () => (await frame.boundingBox())!.y).toBe(0);
  await ctx.close();
});

test("an expired view says to open it again, under the header", async ({ browser }, testInfo) => {
  const { ctx, page } = await operator(browser, testInfo, "expired");
  await page.goto(O);
  await expect(page.getByTestId("artifact-unavailable")).toContainText("This view has expired");
  await expect(page.getByTestId("public-bar")).toBeVisible();
  await expect(page.getByTestId("artifact-frame")).toHaveCount(0);
  await ctx.close();
});

test("a tab says Sec-Fetch-Dest: document; a frame and an <img> of the same link do not", async ({ browser }, testInfo) => {
  // The server opens the viewer only for `document`; this is the browser half of that contract.
  // A context with NO routes: once any route is registered Playwright intercepts every request,
  // and an intercepted request never shows the Sec-Fetch-* headers the browser adds afterwards.
  // The preview server answers the link however it likes; only what the browser sent matters.
  const base = testInfo.project.use.baseURL!;
  const ctx = await browser.newContext({ baseURL: base });
  const page = await ctx.newPage();
  const LINK = new URL("/artifacts/5449-5592-2107-4372/", base).toString();
  const seen: string[] = [];
  ctx.on("requestfinished", async (req) => {
    if (req.url() !== LINK) return;
    seen.push((await req.allHeaders())["sec-fetch-dest"] ?? "none");
  });
  await page.goto("/");
  await page.setContent(`<iframe src="${LINK}"></iframe><img src="${LINK}">`);
  await expect.poll(() => [...seen].sort()).toEqual(["iframe", "image"]);
  await page.goto(LINK);
  await expect.poll(() => seen.length).toBe(3);
  expect(seen[2]).toBe("document");
  await ctx.close();
});
