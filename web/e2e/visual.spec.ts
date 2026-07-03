/**
 * Visual capture (#96, Phase 1) — drives the real /login + each registered area at every
 * screen format and writes PNGs + manifest.json to VISUAL_OUT, so UI is reviewed against
 * real renders. Captures against an already-running app (E2E_BASE_URL); Phase 2 adds the
 * seeded ephemeral instance. Single browser, viewport sizes driven per-shot, so run with
 * one project: `playwright test e2e/visual.spec.ts --project=desktop` (the `visual` script).
 *
 *   E2E_BASE_URL=https://your-domain.example VISUAL_USER=admin VISUAL_PASS=… \
 *   VISUAL_OUT=/abs/out VISUAL_AREAS=all HEAD_SHA=$(git rev-parse HEAD) \
 *   npm run visual
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { test } from "@playwright/test";
import type { BrowserContext, Page } from "@playwright/test";
import { VISUAL_PATHS, VIEWPORTS, VIEWPORT_NAMES, type VisualPath } from "../visual/paths";
import {
  parseAreasArg,
  shotFile,
  emptyManifest,
  type ManifestEntry,
} from "../visual/manifest";

const OUT = process.env.VISUAL_OUT ?? "visual-out";
const USER = process.env.VISUAL_USER ?? "admin";
const PASS = process.env.VISUAL_PASS ?? "";
const BASE = process.env.E2E_BASE_URL ?? "http://localhost:4173";

async function waitForReady(page: Page, p: VisualPath): Promise<void> {
  const w = p.waitFor;
  if ("selector" in w) {
    await page.waitForSelector(w.selector, { timeout: w.timeoutMs ?? 8000, state: "visible" });
  } else if ("kind" in w) {
    await page.waitForLoadState("networkidle", { timeout: w.timeoutMs ?? 5000 });
  } else {
    await page.waitForTimeout(w.timeoutMs);
  }
}

/** Log in via the server /login form. Returns true iff the session cookie was set. */
async function login(ctx: BrowserContext): Promise<boolean> {
  const page = await ctx.newPage();
  try {
    await page.goto("/login", { waitUntil: "domcontentloaded" });
    await page.fill('input[name="username"]', USER);
    await page.fill('input[name="password"]', PASS);
    await page.click('button[type="submit"]');
    await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
    const cookies = await ctx.cookies();
    return cookies.some((c) => c.name === "agent_sessions");
  } finally {
    await page.close().catch(() => {});
  }
}

test("visual capture", async ({ browser }, info) => {
  // Needs a real running app + creds. Skip in the default `test:e2e` run (static preview,
  // no backend → no server /login form); it runs via `npm run visual` / CI Phase 3 with
  // E2E_BASE_URL + VISUAL_PASS set.
  test.skip(
    !process.env.E2E_BASE_URL || !process.env.VISUAL_PASS,
    "visual capture needs E2E_BASE_URL + VISUAL_PASS (run via `npm run visual`)",
  );
  test.setTimeout(180_000);
  const parsed = parseAreasArg(process.env.VISUAL_AREAS ?? "all");
  if (parsed.kind === "none") {
    // Still write an EMPTY manifest so the comment poster patches the snapshot to
    // "scope: none / no rows" instead of leaving a stale prior comment in place.
    mkdirSync(OUT, { recursive: true });
    writeFileSync(
      `${OUT}/manifest.json`,
      JSON.stringify(emptyManifest(BASE, process.env.HEAD_SHA ?? null, []), null, 2) + "\n",
    );
    info.skip(true, "VISUAL_AREAS empty/none — wrote empty manifest");
    return;
  }
  if (parsed.kind === "invalid") throw new Error(`unknown areas: ${parsed.unknown.join(", ")}`);
  const areas = new Set(parsed.areas);
  const inScope = VISUAL_PATHS.filter((p) => areas.has(p.name));

  mkdirSync(OUT, { recursive: true });
  const manifest = emptyManifest(BASE, process.env.HEAD_SHA ?? null, parsed.areas);

  // One anonymous context (login page) + one authed context (everything else). Motion is left
  // ON so the ambient HUD canvas + LEDs + button glitch render in the shots (#211) — these are
  // review-grade captures (eyeballed by Hermes + the operator), not pixel-diffed, so the
  // canvas's randomness is fine.
  const anon = await browser.newContext({ baseURL: BASE });
  const authed = await browser.newContext({ baseURL: BASE });
  // Authenticate the authed context. Prefer a directly-minted session cookie (set by
  // run-local.sh from the known SECRET_KEY via the app's own signer) — faithful, reliable, and
  // 2FA-agnostic, with no flaky form round-trip. Fall back to the server /login form when no
  // cookie is supplied (e.g. capturing a remote instance).
  const sessionCookie = process.env.VISUAL_SESSION_COOKIE;
  let loggedIn = true;
  if (inScope.some((p) => p.requireAuth === "admin")) {
    if (sessionCookie) {
      await authed.addCookies([
        { name: "agent_sessions", value: sessionCookie, url: BASE, httpOnly: true, sameSite: "Lax" },
      ]);
    } else {
      loggedIn = await login(authed);
    }
  }

  for (const p of inScope) {
    const ctx = p.requireAuth === "admin" ? authed : anon;
    const role = p.requireAuth === "admin" ? "admin" : "anonymous";
    for (const vp of VIEWPORT_NAMES) {
      const file = shotFile(p.name, vp);
      const entry: ManifestEntry = {
        name: p.name,
        viewport: vp,
        url: `${BASE}${p.path}`,
        role,
        status: "ok",
        file,
        duration_ms: 0,
      };
      const started = Date.now();
      // A required login that failed → record login_failed, never a blank shot.
      if (p.requireAuth === "admin" && !loggedIn) {
        manifest.paths.push({ ...entry, status: "login_failed", file: null, error: "login failed" });
        continue;
      }
      const page = await ctx.newPage();
      try {
        await page.setViewportSize(VIEWPORTS[vp]);
        await page.goto(p.path, { waitUntil: "domcontentloaded", timeout: 20000 });
        await waitForReady(page, p);
        // Guard against the demoapp "login-redirect screenshot" quirk: an authed area that
        // rendered the server /login form means auth didn't take — fail the shot rather than
        // capture a misleading login page as e.g. "settings".
        if (p.requireAuth === "admin" && (await page.locator('form[action="/login"]').count()) > 0) {
          throw new Error("auth not applied — rendered the /login form");
        }
        // Settle so the motion-on canvas + LEDs have painted a frame before the shot.
        await page.waitForTimeout(600);
        await page.screenshot({ path: `${OUT}/${file}`, fullPage: false });
        entry.duration_ms = Date.now() - started;
        manifest.paths.push(entry);
      } catch (err) {
        manifest.paths.push({
          ...entry,
          status: "failed",
          file: null,
          duration_ms: Date.now() - started,
          error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
        });
      } finally {
        await page.close().catch(() => {});
      }
    }
  }

  await anon.close();
  await authed.close();
  writeFileSync(`${OUT}/manifest.json`, JSON.stringify(manifest, null, 2) + "\n");

  const ok = manifest.paths.filter((p) => p.status === "ok").length;
  console.log(`[visual] captured ${ok}/${manifest.paths.length} → ${OUT}/manifest.json`);
  // Fail loudly on ANY required miss — every in-scope, non-seeded shot must be `ok`.
  // (Manifest is already written above, so diagnostics survive the failure.) Seeded
  // areas are skipped until the Phase-2 seeder exists. This stops a broken admin login
  // from passing just because the public /login area captured (Hermes #99).
  const seeded = new Set(VISUAL_PATHS.filter((p) => p.seeded).map((p) => p.name));
  const required = manifest.paths.filter((p) => !seeded.has(p.name));
  const bad = required.filter((p) => p.status !== "ok");
  if (bad.length > 0) {
    const detail = bad.map((p) => `${p.name}/${p.viewport}=${p.status}`).join(", ");
    throw new Error(`visual capture: ${bad.length}/${required.length} required shots not ok → ${detail}`);
  }
});
