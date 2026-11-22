import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
  PY_STACK_AVAILABLE,
  SESSION_UUID,
  startStack,
  type Stack,
} from "./appmode";

/** A hosted artifact in a REAL browser against the REAL server (#1932 P1).
 *
 *  Every `/artifacts` response is `CSP: sandbox` without `allow-same-origin`, so the page runs in an
 *  opaque origin. That must hold BOTH ways, and only a browser enforces either:
 *
 *  - the page's own relative assets (a stylesheet, an image, an external script) load under the
 *    view-token prefix, with no cookie and no same-origin relationship (a `Cross-Origin-Resource-
 *    Policy: same-origin` header once blocked every one of them — Hermes on PR #1933);
 *  - its scripts still cannot read BattleLab's cookie or read any `/api` answer.
 */
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(({ isMobile }) => !!isMobile, "runs once, on desktop");
test.skip(
  !PY_STACK_AVAILABLE,
  "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml",
);

const MEDIA = join(process.cwd(), "e2e", "fixtures", "media");
const SESSION = `claude:${SESSION_UUID}`;

let stack: Stack;
let id = "";
let mdId = "";
const MD_TEXT = readFileSync(
  join(process.cwd(), "e2e", "fixtures", "artifact-md", "report.md"),
  "utf8",
);

test.beforeAll(async () => {
  test.setTimeout(240_000);
  stack = await startStack();
  const site = join(stack.repo, "site");
  mkdirSync(join(site, "img"), { recursive: true });
  writeFileSync(
    join(site, "index.html"),
    `<!doctype html><html><head><meta charset="utf-8"><title>Probe</title>
<link rel="stylesheet" href="style.css"></head>
<body><h1 id="h">Probe site</h1><img id="dot" src="img/dot.png" alt="dot">
<script src="app.js"></script></body></html>`,
  );
  writeFileSync(join(site, "style.css"), "h1 { color: rgb(1, 2, 3); }\n");
  writeFileSync(
    join(site, "img", "dot.png"),
    readFileSync(join(MEDIA, "dot.png")),
  );
  writeFileSync(
    join(site, "app.js"),
    `document.body.dataset.ran = "1";
try { document.body.dataset.cookie = "read:" + document.cookie; }
catch (e) { document.body.dataset.cookie = "blocked"; }
fetch("/api/config", { credentials: "include" })
  .then((r) => r.text())
  .then((t) => { document.body.dataset.api = "read:" + t.slice(0, 40); })
  .catch(() => { document.body.dataset.api = "blocked"; });
`,
  );
  const out = execFileSync(
    "uv",
    ["run", "python", "tests/appmode/seed_artifacts.py", stack.repo, SESSION],
    {
      cwd: resolve(process.cwd(), ".."),
      env: stack.env,
      encoding: "utf8",
    },
  );
  ({ id } = JSON.parse(out.trim().split("\n").pop()!));
  // #2210: a Markdown report, published as a single file.
  writeFileSync(join(stack.repo, "report.md"), MD_TEXT);
  const md = execFileSync(
    "uv",
    [
      "run",
      "python",
      "tests/appmode/seed_artifacts.py",
      stack.repo,
      SESSION,
      "report.md",
      "Operating model",
    ],
    { cwd: resolve(process.cwd(), ".."), env: stack.env, encoding: "utf8" },
  );
  ({ id: mdId } = JSON.parse(md.trim().split("\n").pop()!));
});

test.afterAll(async () => {
  await stack?.stop();
});

const base = () => `http://127.0.0.1:${stack.appPort}`;

/** Open the operator's stable link in a tab, as Open does. Since #2270 that lands on the viewer
 *  page, `/artifacts/o/<view token>/`, with the BattleLab header above the artifact framed from
 *  `/artifacts/v/<token>/` — the real server choosing it from the real browser's
 *  `Sec-Fetch-Dest: document`. Then load that framed page itself at the top level, so the checks
 *  below see the hosted page exactly as the frame does (same URL, same headers). */
async function openHosted(page: Page, artifactId: string) {
  await page.goto(`${base()}/artifacts/${artifactId}/`);
  expect(new URL(page.url()).pathname).toMatch(/^\/artifacts\/o\/[^/]+\/$/);
  const token = new URL(page.url()).pathname.split("/")[3];
  await expect(page.getByTestId("public-bar")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("artifact-frame")).toHaveAttribute(
    "src",
    `/artifacts/v/${token}/`,
  );
  return page.goto(`${base()}/artifacts/v/${token}/`);
}

test("a hosted site loads its own assets and still cannot reach the cookie or /api", async ({
  page,
}) => {
  const resp = await openHosted(page, id);
  expect(resp?.status()).toBe(200);
  // The stable link handed the browser a view token; the page lives under it.
  expect(new URL(page.url()).pathname).toMatch(/^\/artifacts\/v\/[^/]+\/$/);
  const csp = resp!.headers()["content-security-policy"];
  expect(csp.startsWith("sandbox ")).toBe(true);
  expect(csp).not.toContain("allow-same-origin");

  // The stylesheet applied, the image decoded, the external script ran.
  await expect
    .poll(
      () => page.locator("#h").evaluate((el) => getComputedStyle(el).color),
      { timeout: 30_000 },
    )
    .toBe("rgb(1, 2, 3)");
  await expect
    .poll(
      () =>
        page
          .locator("#dot")
          .evaluate((el: HTMLImageElement) => el.naturalWidth),
      { timeout: 30_000 },
    )
    .toBeGreaterThan(0);
  await expect(page.locator("body")).toHaveAttribute("data-ran", "1", {
    timeout: 30_000,
  });

  // …and the sandbox still holds: no cookie, no readable /api answer.
  await expect(page.locator("body")).toHaveAttribute("data-cookie", "blocked");
  await expect(page.locator("body")).toHaveAttribute("data-api", "blocked", {
    timeout: 30_000,
  });
});

test("a Markdown artifact opens as a rendered document, in a tab and in the preview frame (#2210)", async ({
  page,
}) => {
  // Opened as a page (top level): the server's document page, the renderer from /assets.
  const resp = await openHosted(page, mdId);
  expect(resp?.status()).toBe(200);
  expect(resp!.headers()["content-type"]).toMatch(/^text\/html/);
  const csp = resp!.headers()["content-security-policy"];
  expect(csp.startsWith("sandbox ")).toBe(true);
  expect(csp).not.toContain("allow-same-origin");
  expect(resp!.headers()["access-control-allow-origin"]).toBeUndefined();
  await expect(page.locator("body")).toHaveAttribute("data-md", "rendered", {
    timeout: 30_000,
  });
  const doc = page.locator("#md-doc");
  await expect(doc.getByRole("heading", { level: 1 })).toHaveText(
    "Operating model — concept map",
  );
  await expect(doc.locator("table tbody tr")).toHaveCount(3);
  // Still an opaque origin: no cookie, no storage, and the raw HTML in the source never ran.
  expect(await page.evaluate(() => self.origin)).toBe("null");
  expect(
    await page.evaluate(() => {
      try {
        localStorage.setItem("x", "1");
        return "read";
      } catch {
        return "blocked";
      }
    }),
  ).toBe("blocked");
  await expect(page.locator("body")).not.toHaveAttribute("data-injected", "1");
  // The renderer and the device-theme tokens loaded (a module script would have been refused).
  expect(
    await page.evaluate(() => getComputedStyle(document.body).backgroundColor),
  ).not.toBe("rgba(0, 0, 0, 0)");

  // Source is the stored text, exactly.
  await page.getByRole("button", { name: "Source" }).click();
  await expect(page.locator("#md-source")).toBeVisible();
  expect(await page.locator("#md-source").textContent()).toBe(MD_TEXT);

  // Download: the stored bytes, as an attachment.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: /Download/ }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("report.md");
  expect(readFileSync((await download.path())!, "utf8")).toBe(MD_TEXT);

  // A fetch (not a page) still gets the bytes as text/plain.
  const raw = await page.request.get(page.url().split("?")[0]);
  expect(raw.headers()["content-type"]).toBe("text/plain; charset=utf-8");
  expect(await raw.text()).toBe(MD_TEXT);

  // The operator's preview frame (sandboxed, never allow-same-origin) shows the document too.
  await page.goto(`${base()}/library/artifacts/${encodeURIComponent(mdId)}`);
  const frame = page.frameLocator("[data-testid=artifact-preview]");
  await expect(frame.locator("body")).toHaveAttribute("data-md", "rendered", {
    timeout: 30_000,
  });
  await expect(
    frame.locator("#md-doc").getByRole("heading", { level: 1 }),
  ).toHaveText("Operating model — concept map");
});
