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
// A fake microphone, granted only where a capture asks (`grant: ["microphone"]`): the call panel
// gets past the microphone to the state it is meant to show (#1640 review 6912).
const browser = await chromium.launch({
  args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
});
const failures = [];
for (const cap of SWEEP.filter((c) => !ONLY.length || ONLY.includes(c.id))) {
  // A page the Python server renders (#1596): photographed by its own real-server spec, not here.
  if (cap.served) {
    const run = `NIGHTJAR_CAPTURE_DIR=… npx playwright test ${cap.served}`;
    console.log(`  ${cap.id}: served page — ${cap.cwd ? `(cd ${cap.cwd} && ${run})` : run}`);
    continue;
  }
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
        // `init`: a script the page sees before its own (the connect page's test harness seam).
        if (cap.init) await ctx.addInitScript({ content: cap.init });
        const page = await ctx.newPage();
        // `document`: the HTML file the server answers this path with (share.html at /p/<token>).
        if (cap.document)
          await page.route(
            (u) => u.pathname === cap.path,
            async (r) => r.fulfill({ response: await r.fetch({ url: new URL(cap.document, BASE).toString() }) }),
          );
        const mockState = { theme, unhandled: new Set(), ...(cap.mock ?? {}) };
        await routeApi(page, mockState);
        await routeTerminal(page, cap.term ?? {});
        // A held asset never answers: the route's chunk is "still loading" when the shot is taken.
        if (cap.holdAsset) await page.route(new RegExp(cap.holdAsset), () => {});
        // Per-capture answers, registered AFTER the shared handler so they win (the last route
        // registered for a URL is asked first): a state the shared fixtures do not hold.
        // A body `{ status, json }` answers that status; anything else is the JSON body. The path
        // matches with or without a query string.
        // A `{ status, json, method }` body answers only that method; any other falls through to
        // the shared handler (a refused PATCH on a path whose GET still reads the list).
        const answer = async (apiMap) => {
          for (const [path, body] of Object.entries(apiMap ?? {})) {
            // `re:` keys are a raw regex (a path with an id in it); any other key is a literal path.
            const re = path.startsWith("re:")
              ? new RegExp(path.slice(3))
              : new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\?|$)`);
            await page.route(re, (r) => {
              const shaped = body && typeof body === "object" && "status" in body && "json" in body;
              if (shaped && body.method && r.request().method() !== body.method) return r.fallback();
              return shaped ? r.fulfill({ status: body.status, json: body.json }) : r.fulfill({ json: body });
            });
          }
        };
        await answer(cap.api);
        const name = `${cap.id.replace(/[:/]/g, "_")}-${width}-${theme}-${dpr}x`;
        let held = false; // a `drag` step holds the pointer down across the shot
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
            // `{ api: {...} }` mid-sequence: answer these paths differently FROM HERE ON (a later
            // route wins), for a state that a re-read produces (the list emptied under a window).
            if (step.api) {
              await answer(step.api);
              continue;
            }
            // `{ hold: ms, css }`: press and hold the target (the connect page's human gate).
            if (step.hold) {
              const box = await page.locator(step.css).first().boundingBox();
              if (!box) throw new Error("hold target has no box");
              await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
              await page.mouse.down();
              await page.waitForTimeout(step.hold);
              await page.mouse.up();
              continue;
            }
            if (step.press) {
              await page.keyboard.press(step.press);
              await page.waitForTimeout(300);
              continue;
            }
            if (step.viewport) {
              await page.setViewportSize({ width: step.viewport[0], height: step.viewport[1] });
              await page.waitForTimeout(300);
              continue;
            }
            const target =
              "testId" in step
                ? page.getByTestId(step.testId).first()
                : "css" in step
                  ? page.locator(step.css).first()
                  : page.getByRole(step.role, { name: step.name, exact: !step.partial }).first();
            if (step.wait) {
              await target.waitFor({ state: step.wait, timeout: 15000 });
              continue;
            }
            if (step.fill !== undefined) {
              await target.fill(step.fill);
              continue;
            }
            if (step.select !== undefined) {
              await target.selectOption({ label: step.select });
              continue;
            }
            if (step.drag) {
              // Press on the target's centre and move in small steps (past any activation
              // distance), then HOLD: the shot is the drag in flight; released after it.
              await target.scrollIntoViewIfNeeded();
              const box = await target.boundingBox();
              if (!box) throw new Error("drag target has no box");
              const x = box.x + box.width / 2;
              const y = box.y + box.height / 2;
              await page.mouse.move(x, y);
              await page.mouse.down();
              held = true;
              await page.mouse.move(x + step.drag[0], y + step.drag[1], { steps: 12 });
              await page.waitForTimeout(300);
              continue;
            }
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
          // A state reached through a read (a filter's refetch, a refused write) lands a beat after
          // the last step: wait for it, briefly, before calling it absent.
          if (cap.present) await page.locator(cap.present).first().waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
          // `present` names what the shot is evidence of: bring it into view (a no-op when it is),
          // including inside a scrolling band such as the console's details.
          if (cap.present) await page.locator(cap.present).first().scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
          if (cap.present && !(await page.locator(cap.present).first().isVisible()))
            throw new Error(`${cap.present} is not on screen at capture time`);
          // …and IN the shot: isVisible() passes a state below the fold, which the PNG would not
          // show. A full-page capture takes the whole page, so there the box only has to exist.
          if (cap.present && !cap.fullPage) {
            const box = await page.locator(cap.present).first().boundingBox();
            const vp = page.viewportSize();
            if (!box || !vp || box.y >= vp.height || box.y + box.height <= 0 || box.x >= vp.width || box.x + box.width <= 0)
              throw new Error(`${cap.present} is outside the shot at capture time`);
            // Not clipped away by a scrolling or clipping ancestor either.
            const unclipped = await page.locator(cap.present).first().evaluate((el) => {
              const r = el.getBoundingClientRect();
              for (let p = el.parentElement; p; p = p.parentElement) {
                const st = getComputedStyle(p);
                if (!/(hidden|auto|scroll|clip)/.test(st.overflowX + st.overflowY)) continue;
                const q = p.getBoundingClientRect();
                if (r.bottom <= q.top || r.top >= q.bottom || r.right <= q.left || r.left >= q.right) return false;
              }
              return true;
            });
            if (!unclipped) throw new Error(`${cap.present} is clipped out of the shot by a scrolling ancestor`);
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
        } finally {
          if (held) await page.mouse.up().catch(() => {});
        }
        await ctx.close();
      }
}
await browser.close();
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
