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

const widths = FULL ? WIDTHS : [1440, 390];
const ratios = FULL ? PIXEL_RATIOS : [1];
const browser = await chromium.launch();
const failures = [];
for (const cap of SWEEP.filter((c) => !ONLY.length || ONLY.includes(c.id))) {
  for (const width of widths)
    for (const theme of THEMES)
      for (const dpr of ratios) {
        const phone = width < 800;
        const ctx = await browser.newContext({
          viewport: { width, height: phone ? 844 : 960 },
          deviceScaleFactor: dpr,
          isMobile: phone,
          hasTouch: phone,
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
        const page = await ctx.newPage();
        await routeApi(page, { theme, unhandled: new Set() });
        await routeTerminal(page, {});
        const name = `${cap.id.replace(/[:/]/g, "_")}-${width}-${theme}-${dpr}x`;
        try {
          await page.goto(cap.path);
          if (cap.ready) await page.getByTestId(cap.ready).first().waitFor({ timeout: 15000 });
          await page.waitForLoadState("networkidle").catch(() => {});
          await page.waitForTimeout(cap.action ? 300 : 800);
          if (cap.action === "stream") {
            await page.getByRole("button", { name: "Stream an answer" }).click();
            // Mid-stream: the caret is riding the end and words are still fading in.
            await page.getByTestId("g-stream").locator(".nj-caret").waitFor({ timeout: 10000 });
            await page.waitForTimeout(700);
          } else if (cap.action === "arrive") {
            await page.getByRole("button", { name: "A unit arrives" }).click();
            await page.waitForTimeout(150); // inside the 500 ms arrival flash
          }
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
