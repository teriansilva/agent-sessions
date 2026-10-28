import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { extname, join, normalize, resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";

import { connect, fillAndConnect, openSessions, PY_STACK_AVAILABLE, startStack, type Stack } from "./appmode";

/** #1735 over the REAL path: the public connect build (base `/connect/`), the real relay, agent
 *  and app. The landing vhost is emulated by a loopback server with its exact `/connect/` rules:
 *  `try_files $uri $uri/ /connect/index.html` for pages, `=404` for `/connect/assets/`. Before the
 *  fix, connecting moved the address bar to `/dashboard`, outside `/connect/`, so every reload
 *  left the app (the landing host 301s everything else away, to a 404). */
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv) — see .forgejo/workflows/appmode-e2e.yml");

const DIST = resolve(process.cwd(), "dist-connect");
const TYPES: Record<string, string> = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
  ".png": "image/png", ".jpg": "image/jpeg", ".webm": "video/webm", ".mp4": "video/mp4",
  ".json": "application/json", ".woff2": "font/woff2", ".webmanifest": "application/manifest+json",
};

/** The landing vhost's `/connect/` locations, served from `dist-connect` (index = connect.html) by
 *  a real loopback server — a routed (fulfilled) page counts as a public origin to Chromium's
 *  local-network checks and could not reach the loopback relay. */
function vhost(): Promise<{ origin: string; close: () => Promise<void> }> {
  // Built here, every run: the spec grades exactly the bundle it serves, never a stale one.
  execFileSync("npm", ["run", "build:connect"], { stdio: "pipe" });
  if (!existsSync(join(DIST, "connect.html"))) throw new Error("build:connect produced no connect.html");
  const server = createServer((req, res) => {
    const path = new URL(req.url ?? "/", "http://x").pathname;
    const send = (status: number, file: string | null) => {
      if (file === null) return void res.writeHead(status).end("not found");
      res.writeHead(status, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
      res.end(readFileSync(file));
    };
    if (path === "/connect") return void res.writeHead(301, { location: "/connect/" }).end();
    if (!path.startsWith("/connect/")) return send(404, null);
    const rel = normalize(decodeURIComponent(path.slice("/connect/".length)));
    const file = rel && rel !== "." && !rel.startsWith("..") ? join(DIST, rel) : "";
    if (file && existsSync(file) && statSync(file).isFile()) return send(200, file);
    // `location /connect/assets/ { try_files $uri =404; }`
    if (path.startsWith("/connect/assets/")) return send(404, null);
    // `location /connect/ { try_files $uri $uri/ /connect/index.html; }`
    return send(200, join(DIST, "connect.html"));
  });
  return new Promise((ok) =>
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      ok({ origin: `http://127.0.0.1:${port}`, close: () => new Promise((done) => server.close(() => done())) });
    }),
  );
}

let stack: Stack;
let site: Awaited<ReturnType<typeof vhost>>;

test.beforeAll(async () => {
  test.setTimeout(420_000); // the connect build plus the stack
  site = await vhost();
  stack = await startStack();
});

test.afterAll(async () => {
  await stack?.stop();
  await site?.close();
});

const gateShows = async (page: Page) => {
  // The reload stops at a fresh, unverified gate: nothing auto-connects (#690).
  await expect(page.locator("body")).not.toHaveAttribute("data-state", "connected");
  await expect(page.locator("#verify-gate")).not.toHaveAttribute("data-state", "verified");
  await expect(page.getByRole("button", { name: /Connect/ })).toBeVisible();
};

test("a reload through the relay returns to the same route after the connect gate", async ({ page }) => {
  await connect(page, stack, `${site.origin}/connect/`);
  // The app's URLs stay under the base (red before the fix: `/dashboard`).
  await expect.poll(() => new URL(page.url()).pathname).toBe("/connect/dashboard");

  await openSessions(page);
  const route = new URL(page.url()).pathname;
  expect(route.startsWith("/connect/") && route !== "/connect/dashboard").toBe(true);

  await page.reload();
  await gateShows(page);
  expect(new URL(page.url()).pathname).toBe(route);

  await fillAndConnect(page, stack);
  await expect(page.getByRole("heading", { level: 1, name: "Start a new session" })).toBeVisible({ timeout: 30_000 });
  expect(new URL(page.url()).pathname).toBe(route);
});

test("with expired stored credentials the reload asks for them again, then reopens the route", async ({ page }) => {
  await connect(page, stack, `${site.origin}/connect/`);
  await openSessions(page);
  const route = new URL(page.url()).pathname;
  // The stored relay session is gone (expired / another browser profile): nothing to restore.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await gateShows(page);
  await expect(page.locator("#key")).toHaveValue("");
  await fillAndConnect(page, stack);
  await expect(page.getByRole("heading", { level: 1, name: "Start a new session" })).toBeVisible({ timeout: 30_000 });
  expect(new URL(page.url()).pathname).toBe(route);
});

test("route query state survives the connect; anything else in the URL does not", async ({ page }) => {
  // Record every address the shell canonicalises to, before the app may move on.
  await page.addInitScript(() => {
    const seen: string[] = [];
    (window as unknown as { __urls: string[] }).__urls = seen;
    const replace = history.replaceState.bind(history);
    history.replaceState = (data, unused, url) => {
      if (url != null) seen.push(String(url));
      return replace(data, unused, url);
    };
  });
  const mission = `msn_${"a".repeat(32)}`;
  await connect(page, stack, `${site.origin}/connect/mission?m=${mission}&key=leaked#frag`);
  const urls = await page.evaluate(() => (window as unknown as { __urls: string[] }).__urls);
  expect(urls[0]).toBe(`/connect/mission?m=${mission}`);
  expect(page.url()).not.toContain("leaked");
});

test("server pages and missing chunks under /connect/ never loop or answer with HTML", async ({ page }) => {
  let navigations = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) navigations += 1;
  });
  await page.goto(`${site.origin}/connect/login`);
  await gateShows(page);
  await page.waitForTimeout(1500);
  expect(navigations).toBe(1);
  expect(new URL(page.url()).pathname).toBe("/connect/login");
  const missing = await page.evaluate(async () => (await fetch("/connect/assets/missing-chunk.js")).status);
  expect(missing).toBe(404);
});
