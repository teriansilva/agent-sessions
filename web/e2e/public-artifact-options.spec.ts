import { expect, test, type Browser, type TestInfo } from "@playwright/test";
import { TOUCH_FLOOR } from "./touchTarget";

// #2217: a public artifact link's options, in a real browser — a password card that shows nothing
// of the artifact before the right password, and a link shared without the BattleLab header.
//
// As in public-artifact.spec.ts the visitor is a FRESH context holding a BattleLab cookie it must
// never leak; it is served the built `artifact.html`, the link's `~meta` / `~unlock` as the server
// answers them, and the artifact's page. A password link's page lives under `~u/<token>/`, never
// `~/`, and is served WITHOUT the server's `CSP: sandbox` on purpose: the frame's own sandbox is
// what this proves holds behind the unlock too.

const ID = "Ar7fQ2xAAAAAAAAAAAAAAA";
const P = `/artifacts/p/${ID}/`;
const SECRET = "operator-secret-value";
const TOKEN = "eyJwIjoiQXI3In0.Zz-_9.sig";

const PROBE = `<!doctype html><html><head><meta charset="utf-8"><title>Probe</title></head>
<body style="margin:0;padding:24px;font:16px system-ui"><h1 id="h">Q4 latency report</h1>
<script>
const b = document.body.dataset;
try { b.cookie = "read:" + document.cookie; } catch (e) { b.cookie = "blocked"; }
try { localStorage.setItem("x", "1"); b.storage = "read"; } catch (e) { b.storage = "blocked"; }
try { b.parent = "read:" + window.parent.document.title; } catch (e) { b.parent = "blocked"; }
</script></body></html>`;

type Link = { password?: string; header?: boolean };

async function visitor(browser: Browser, testInfo: TestInfo, link: Link) {
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
  const header = link.header !== false;
  const meta = { title: "Q4 latency report", kind: "site", updated_at: Date.now() / 1000 - 7200, header };
  const unlocks: { password: string; cookie: string }[] = [];
  const plainContent: string[] = [];
  await ctx.route("**/api/**", (r) => r.fulfill({ status: 599, json: { detail: "a route the viewer must not call" } }));
  await ctx.route((u) => u.pathname === P, async (r) => {
    const response = await r.fetch({ url: new URL("/artifact.html", r.request().url()).toString() });
    await r.fulfill({ response });
  });
  await ctx.route((u) => u.pathname === `${P}~meta`, (r) =>
    link.password
      ? r.fulfill({ status: 401, json: { detail: "password required", password_required: true, header } })
      : r.fulfill({ json: meta }),
  );
  let left = 5;
  await ctx.route((u) => u.pathname === `${P}~unlock`, (r) => {
    const password = JSON.parse(r.request().postData() ?? "{}").password as string;
    unlocks.push({ password, cookie: r.request().headers()["cookie"] ?? "" });
    if (password !== link.password)
      return r.fulfill({ status: 401, json: { detail: "wrong password", password_required: true, tries_left: --left } });
    return r.fulfill({ json: { ...meta, token: TOKEN } });
  });
  // A password link's content is ONLY under its token; `~/` answers the server's 404.
  await ctx.route((u) => u.pathname === `${P}~/`, (r) => {
    plainContent.push(r.request().url());
    return link.password
      ? r.fulfill({ status: 404, contentType: "text/plain", body: "not found\n" })
      : r.fulfill({ status: 200, contentType: "text/html", body: PROBE });
  });
  await ctx.route((u) => u.pathname === `${P}~u/${TOKEN}/`, (r) =>
    r.fulfill({ status: 200, contentType: "text/html", body: PROBE }),
  );
  return { ctx, page: await ctx.newPage(), unlocks, plainContent };
}

test("a password link shows its card and nothing of the artifact; the right password frames it, still sandboxed", async ({ browser }, testInfo) => {
  const { ctx, page, unlocks, plainContent } = await visitor(browser, testInfo, { password: "open sesame" });
  await page.goto(P);
  const gate = page.getByTestId("artifact-password-gate");
  await expect(gate).toContainText("This link needs a password");
  await expect(page.getByTestId("public-bar-brand")).toBeVisible();
  await expect(page.getByTestId("public-bar")).not.toContainText("Q4 latency report");
  await expect(page.getByTestId("artifact-frame")).toHaveCount(0);
  if (testInfo.project.name === "mobile") {
    for (const control of [page.getByLabel("Password"), gate.getByRole("button", { name: "Show" }), gate.getByRole("button", { name: "Open" })]) {
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }

  await page.getByLabel("Password").fill("wrong");
  await gate.getByRole("button", { name: "Open" }).click();
  await expect(gate.getByRole("alert")).toHaveText("Wrong password. 4 tries left before a 15-minute pause.");
  await expect(page.getByTestId("artifact-frame")).toHaveCount(0);

  await page.getByLabel("Password").fill("open sesame");
  await page.getByLabel("Password").press("Enter");
  const frame = page.getByTestId("artifact-frame");
  await expect(frame).toHaveAttribute("src", `${P}~u/${TOKEN}/`);
  await expect(page.getByTestId("public-bar")).toContainText("Q4 latency report");
  const body = page.frameLocator("[data-testid=artifact-frame]").locator("body");
  await expect(body.locator("#h")).toHaveText("Q4 latency report");
  // Behind the unlock the frame is the same opaque origin: no cookie, storage or parent page.
  await expect(body).toHaveAttribute("data-cookie", "blocked");
  await expect(body).toHaveAttribute("data-storage", "blocked");
  await expect(body).toHaveAttribute("data-parent", "blocked");
  // The unlock sent no cookie; the token never reaches the address bar or storage.
  expect(unlocks.map((u) => u.password)).toEqual(["wrong", "open sesame"]);
  expect(unlocks.filter((u) => u.cookie.includes(SECRET))).toEqual([]);
  expect(new URL(page.url()).pathname).toBe(P);
  expect(page.url()).not.toContain(TOKEN);
  const stored = await page.evaluate(() =>
    [sessionStorage, localStorage].flatMap((st) => Object.keys(st).map((k) => `${k}=${st.getItem(k)}`)).join("\n"),
  );
  expect(stored).not.toContain(TOKEN);
  expect(stored).not.toContain("open sesame");
  expect(plainContent).toEqual([]);
  await ctx.close();
});

test("a link shared without the header: the artifact fills the whole viewport under its own title", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo, { header: false });
  await page.goto(P);
  await expect(page.frameLocator("[data-testid=artifact-frame]").locator("#h")).toHaveText("Q4 latency report");
  await expect(page.getByTestId("public-bar")).toHaveCount(0);
  await expect(page).toHaveTitle("Q4 latency report");
  const vp = page.viewportSize()!;
  const box = (await page.getByTestId("artifact-frame").boundingBox())!;
  expect(box.x).toBe(0);
  expect(box.y).toBe(0);
  expect(Math.round(box.width)).toBe(vp.width);
  expect(Math.round(box.height)).toBe(vp.height);
  const sandbox = (await page.getByTestId("artifact-frame").getAttribute("sandbox"))!.split(" ");
  expect(sandbox).not.toContain("allow-same-origin");
  await ctx.close();
});

test("a headerless password link: the card alone, then the artifact edge to edge", async ({ browser }, testInfo) => {
  const { ctx, page } = await visitor(browser, testInfo, { header: false, password: "pw" });
  await page.goto(P);
  await expect(page.getByTestId("artifact-password-gate")).toBeVisible();
  await expect(page.getByTestId("public-bar")).toHaveCount(0);
  await page.getByLabel("Password").fill("pw");
  await page.getByRole("button", { name: "Open" }).click();
  await expect(page.frameLocator("[data-testid=artifact-frame]").locator("#h")).toHaveText("Q4 latency report");
  await expect(page.getByTestId("public-bar")).toHaveCount(0);
  const vp = page.viewportSize()!;
  const box = (await page.getByTestId("artifact-frame").boundingBox())!;
  expect(box.y).toBe(0);
  expect(Math.round(box.height)).toBe(vp.height);
  await ctx.close();
});
