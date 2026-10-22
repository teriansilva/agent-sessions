// The NIGHTJAR screenshot sweep (#1531). Captures the REAL built SPA with `/api/**` mocked by the
// promo fixtures (visual/promo/mockApi.mjs): invented data only, nothing read from this machine.
// Default: desktop 1440 + phone 390, dark + light, 1×, motion Off (stills). `--full` runs every
// width × theme × pixel ratio (the Phase 14 gate). See manifest.mjs for the ids.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";

import { routeApi } from "../promo/mockApi.mjs";
import { routeTerminal } from "../promo/terminal.mjs";
import { PIXEL_RATIOS, SWEEP, THEMES, WIDTHS } from "./manifest.mjs";

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const BASE = opt("base", "http://127.0.0.1:41918");
const OUT = resolve(opt("out", "../out/nightjar"));
const ONLY = opt("only", "").split(",").filter(Boolean);
const FULL = args.includes("--full");
const MOTION = opt("motion", "off");
mkdirSync(OUT, { recursive: true });

// `--widths 390,834,1024`: any widths. With it, a width ≤ 1180 is a touch device (phones and
// tablets), at a tablet's portrait height.
const WIDTHS_OPT = opt("widths", "").split(",").filter(Boolean).map(Number);
const widths = WIDTHS_OPT.length ? WIDTHS_OPT : FULL ? WIDTHS : [1440, 390];
const ratios = FULL ? PIXEL_RATIOS : [1];
const browser = await chromium.launch();
const failures = [];
for (const cap of SWEEP.filter((c) => !ONLY.length || ONLY.includes(c.id))) {
  for (const width of widths.filter((w) => !cap.widths || cap.widths.includes(w < 800 ? "phone" : "desktop")))
    for (const theme of THEMES)
      for (const dpr of ratios) {
        const phone = width < 800;
        const touch = WIDTHS_OPT.length ? width <= 1180 : phone;
        const height = phone ? 844 : touch ? Math.round(width * 1.33) : (cap.height ?? 960);
        const ctx = await browser.newContext({
          viewport: { width, height },
          deviceScaleFactor: dpr,
          isMobile: touch,
          hasTouch: touch,
          colorScheme: theme,
          baseURL: BASE,
          serviceWorkers: "block",
        });
        await ctx.addInitScript(
          ([t, m]) => {
            localStorage.setItem("tr-theme", t);
            localStorage.setItem("tr-motion", m);
            localStorage.setItem("battlelab.linkOpenMode", "fullscreen");
          },
          [theme, cap.motion ?? MOTION],
        );
        // Per-capture storage, applied after the defaults above (`null` removes a key).
        if (cap.storage)
          await ctx.addInitScript((entries) => {
            for (const [k, v] of Object.entries(entries)) {
              if (v === null) localStorage.removeItem(k);
              else localStorage.setItem(k, typeof v === "string" ? v : JSON.stringify(v));
            }
          }, cap.storage);
        if (cap.grant) await ctx.grantPermissions(cap.grant, { origin: BASE });
        if (cap.asPerson)
          await ctx.addInitScript(() => {
            Object.defineProperty(Navigator.prototype, "webdriver", { configurable: true, get: () => false });
          });
        const page = await ctx.newPage();
        const mockState = { theme, unhandled: new Set(), ...(cap.mock ?? {}) };
        await routeApi(page, mockState);
        await routeTerminal(page, cap.term ?? {});
        // A held asset never answers: the route's chunk is "still loading" when the shot is taken.
        if (cap.holdAsset) await page.route(new RegExp(cap.holdAsset), () => {});
        // Per-capture answers, registered AFTER the shared handler so they win (the last route
        // registered for a URL is asked first): a state the shared fixtures do not hold.
        // A body `{ status, json }` answers that status; anything else is the JSON body. The path
        // matches with or without a query string.
        for (const [path, body] of Object.entries(cap.api ?? {})) {
          const re = new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\?|$)`);
          await page.route(re, (r) =>
            body && typeof body === "object" && "status" in body && "json" in body
              ? r.fulfill({ status: body.status, json: body.json })
              : r.fulfill({ json: body }),
          );
        }
        const name = `${cap.id.replace(/[:/]/g, "_")}-${width}-${theme}-${dpr}x`;
        try {
          // A peer tab first (the hand-off stub: another open tab takes the link).
          if (cap.peerPath) {
            const peer = await ctx.newPage();
            await routeApi(peer, mockState);
            await routeTerminal(peer, {});
            await peer.goto(cap.peerPath);
            await peer.waitForLoadState("networkidle").catch(() => {});
            await peer.waitForTimeout(800);
            await page.bringToFront();
          }
          await page.goto(cap.path);
          if (cap.at !== undefined) {
            await page.waitForTimeout(cap.at);
          } else {
            if (cap.ready) await page.getByTestId(cap.ready).first().waitFor({ timeout: 15000 });
            await page.waitForLoadState("networkidle").catch(() => {});
            await page.waitForTimeout(cap.action ? 300 : 800);
          }
          const steps = phone && cap.openPhone ? cap.openPhone : [...(phone ? (cap.phoneFirst ?? []) : []), ...(cap.open ?? [])];
          for (const step of steps) {
            const target =
              "testId" in step
                ? page.getByTestId(step.testId).first()
                : "css" in step
                  ? page.locator(step.css).first()
                  : page.getByRole(step.role, { name: step.name, exact: !step.partial }).first();
            if (step.upload) {
              await target.setInputFiles({ name: step.upload, mimeType: "text/plain", buffer: Buffer.from("notes\n") });
              continue;
            }
            await target.click({ timeout: 10000 });
            // A drawer slides in before its rows can be pressed.
            await page.waitForTimeout(250);
          }
          if (cap.open && cap.ready) await page.getByTestId(cap.ready).first().waitFor({ timeout: 10000 });
          if (cap.expect || cap.expectCss) {
            const opened = cap.expect ? page.getByRole(cap.expect) : page.locator(cap.expectCss);
            await opened.first().waitFor({ state: "visible", timeout: 10000 });
            await page.waitForTimeout(450); // past the deploy-in, so the still is the settled popup
          }
          if (cap.action === "stream") {
            await page.getByRole("button", { name: "Stream an answer" }).click();
            // Mid-stream: the caret is riding the end and words are still fading in.
            await page.getByTestId("g-stream").locator(".nj-caret").waitFor({ timeout: 10000 });
            await page.waitForTimeout(700);
          } else if (cap.action === "arrive") {
            await page.getByRole("button", { name: "A unit arrives" }).click();
            await page.waitForTimeout(150); // inside the 500 ms arrival flash
          }
          // The state the capture names must be ON SCREEN at the moment of the shot (a fixed delay
          // alone proves nothing: the boot is once per visit and dismissable).
          if (cap.present && !(await page.locator(cap.present).first().isVisible()))
            throw new Error(`${cap.present} is not on screen at capture time`);
          if (cap.action === "power-on") {
            // The power-on is one-shot (320 ms): replay it, and photograph it WHILE it runs — the
            // panel's own animations must be running at the moment of the shot, or the capture is
            // not evidence of the state it names. A page clip, so nothing waits for "stable".
            const panel = page.getByTestId("g-panel-power").first();
            await panel.scrollIntoViewIfNeeded();
            await page.getByRole("button", { name: "Power on again" }).click();
            await page.waitForTimeout(90);
            const running = await panel.evaluate(
              (el) => el.getAnimations({ subtree: true }).filter((a) => a.playState === "running").length,
            );
            const box = await panel.boundingBox();
            await page.screenshot({ path: `${OUT}/${name}.png`, clip: box });
            if (!running || !box) throw new Error(`power-on not running at the shot (${running} running)`);
          } else if (cap.section) {
            const byId = page.getByTestId(cap.section).first();
            const target = (await byId.count())
              ? byId
              : page.locator(".nj-panel", { hasText: cap.section }).first();
            // The capture is evidence only if its subject is really there and has a size.
            const box = await target.boundingBox();
            if (!box || box.width < 40 || box.height < 20)
              throw new Error(`section "${cap.section}" missing or empty (${JSON.stringify(box)})`);
            await target.scrollIntoViewIfNeeded();
            await target.screenshot({ path: `${OUT}/${name}.png` });
          } else {
            await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: !!cap.fullPage });
          }
          console.log(`  wrote ${name}`);
        } catch (e) {
          failures.push(`${name}: ${String(e).slice(0, 200)}`);
        }
        await ctx.close();
      }
}
await browser.close();
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
