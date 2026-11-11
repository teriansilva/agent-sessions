import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { expect, test } from "@playwright/test";

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
});

test.afterAll(async () => {
  await stack?.stop();
});

const base = () => `http://127.0.0.1:${stack.appPort}`;

test("a hosted site loads its own assets and still cannot reach the cookie or /api", async ({
  page,
}) => {
  const resp = await page.goto(`${base()}/artifacts/${id}/`);
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
