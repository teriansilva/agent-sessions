// Promo / README / tour screenshots of the BattleLab SPA (#1544).
//
// Captures the REAL built SPA (served by `vite preview`) with the whole `/api/**` surface mocked
// in Playwright — no backend, no agents, nothing read from this machine. Every name in the
// fixtures is invented.
//
//   cd web && npm run build && npx vite preview --port 41917 --strictPort &
//   node visual/promo/capture.mjs --base http://127.0.0.1:41917 --out ../out [--only dashboard,hq]
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";

import * as F from "./fixtures.mjs";
import { routeApi } from "./mockApi.mjs";
import { routeTerminal } from "./terminal.mjs";

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const BASE = opt("base", "http://127.0.0.1:41917");
const OUT = resolve(opt("out", "../out"));
const ONLY = opt("only", "")
  .split(",")
  .filter(Boolean);
mkdirSync(OUT, { recursive: true });

const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 };
const PHONE = {
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 3,
  isMobile: true,
  hasTouch: true,
};

const RUNTIME_HINTS = JSON.stringify({
  v: 1,
  runtimes: Object.fromEntries(F.ROSTER.engines.map((e) => [e.id, String(e.runtime)])),
});

async function newPage(browser, device, { theme = "dark", state = {} } = {}) {
  const ctx = await browser.newContext({
    ...device,
    colorScheme: theme,
    reducedMotion: "reduce",
    baseURL: BASE,
    serviceWorkers: "block",
  });
  await ctx.addInitScript(
    ([theme, hints]) => {
      try {
        localStorage.setItem("tr-theme", theme);
        localStorage.setItem("battlelab.engineRuntimes.v1", hints);
        localStorage.setItem("battlelab.linkOpenMode", "fullscreen");
      } catch {
        /* ignore */
      }
      // No custom cursor in a still.
      const css = document.createElement("style");
      css.textContent = "*{cursor:none!important;caret-color:transparent!important}";
      document.addEventListener("DOMContentLoaded", () => document.head.appendChild(css));
    },
    [theme, RUNTIME_HINTS],
  );
  const page = await ctx.newPage();
  const unhandled = new Set();
  page.on("console", (m) => {
    if (m.type() === "error") console.log(`  [console.error] ${m.text().slice(0, 300)}`);
  });
  page.on("pageerror", (e) => console.log(`  [pageerror] ${String(e).slice(0, 400)}`));
  await routeApi(page, { theme, unhandled, ...state });
  await routeTerminal(page, state);
  return { ctx, page, unhandled };
}

async function settle(page, ms = 1200) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(ms);
}

async function hideHints(page) {
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("body *")) {
      if (el.children.length === 0 && /^Drag a session onto a project/.test(el.textContent ?? "")) {
        (el.closest("[class*='hint'],[class*='Hint']") ?? el).style.visibility = "hidden";
      }
    }
  });
}

async function shoot(page, name) {
  await page.evaluate(() => document.activeElement?.blur?.());
  await page.mouse.move(0, 0).catch(() => {});
  await page.waitForTimeout(150);
  const file = `${OUT}/${name}.png`;
  await page.screenshot({ path: file });
  written.add(name);
  console.log(`  wrote ${file}`);
}

const SHOTS = {
  async dashboard(browser, theme = "dark", name = "dashboard") {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP, { theme });
    await page.goto("/");
    await page.getByRole("heading", { name: "Welcome, Commander." }).waitFor();
    await page.getByTestId("hq-primary-contact").waitFor();
    await page.getByTestId("dashboard-details-toggle").click().catch(() => {});
    await page.getByTestId("recent-work").waitFor().catch(() => {});
    await settle(page, 2000);
    await shoot(page, name);
    report(name, unhandled);
    await ctx.close();
  },
  async hq(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP, { state: { hqMatches: false } });
    await page.goto("/");
    await page.getByTestId("hq-primary-contact").click();
    const panel = page.getByTestId("ask-sidebar");
    await panel.waitFor();
    await panel.getByTestId("composer-input").fill("What needs me right now?");
    await panel.getByTestId("composer-send").click();
    await panel.getByRole("article", { name: /Answer from/ }).waitFor();
    await settle(page, 2500);
    // Show the question at the top of the thread.
    await panel.evaluate((el) => {
      for (const n of el.querySelectorAll("*")) {
        const cs = getComputedStyle(n);
        if (/(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight + 4) n.scrollTop = 0;
      }
    });
    await page.waitForTimeout(400);
    await shoot(page, "hq");
    report("hq", unhandled);
    await ctx.close();
  },
  async sessions(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    const s = F.SESSIONS[0];
    await page.goto(`/s/${s.engine}/${s.uuid}`);
    await page.locator(".xterm-rows").waitFor();
    await settle(page, 2500);
    await shoot(page, "sessions");
    report("sessions", unhandled);
    await ctx.close();
  },
  async "api-session"(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    const s = F.SESSIONS.find((x) => x.engine === "claude-api");
    await page.goto(`/s/${s.engine}/${s.uuid}`);
    await page.getByTestId("structured-pane").waitFor();
    await page.getByTestId("md-code").first().waitFor();
    await settle(page, 1500);
    await shoot(page, "api-session");
    report("api-session", unhandled);
    await ctx.close();
  },
  async missions(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    await page.goto(`/mission?m=${F.M1}`);
    await page.getByTestId("console-title").waitFor();
    await page.getByTestId("detail-context").click();
    await settle(page, 2000);
    await shoot(page, "missions");
    report("missions", unhandled);
    await ctx.close();
  },
  async library(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    await page.goto("/library/playbooks");
    await page.getByTestId("playbooks-page").waitFor();
    await settle(page, 1500);
    await shoot(page, "library");
    report("library", unhandled);
    await ctx.close();
  },
  async files(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    const s = F.SESSIONS[0];
    await page.goto(`/s/${s.engine}/${s.uuid}`);
    await page.locator(".xterm-rows").waitFor();
    await page.locator("[data-head-action='files']").first().click();
    await page.locator("[data-file-panel]").waitFor();
    await page.getByRole("tab", { name: /Git/ }).click();
    await page.locator("[data-git-tab]").waitFor();
    await page.getByRole("button", { name: /Recent commits/i }).click().catch(() => {});
    await settle(page, 2000);
    await shoot(page, "files");
    await page.locator("[data-git-row='src/ingest/router.ts']").click();
    await settle(page, 1500);
    await shoot(page, "files-diff");
    report("files", unhandled);
    await ctx.close();
  },
  async map(browser) {
    const { ctx, page, unhandled } = await newPage(browser, DESKTOP);
    await page.goto("/overview");
    await page.locator(".react-flow__node").first().waitFor();
    await page.getByRole("button", { name: "Collapse session list" }).click();
    await page.getByRole("button", { name: /Expand all/i }).click();
    await page.waitForTimeout(1200);
    await page.locator(".react-flow__controls-fitview").click().catch(() => {});
    await settle(page, 1500);
    await hideHints(page);
    await shoot(page, "map-overview");
    // Pan the clusters up to the top band (a real pane drag, so React Flow keeps its own state).
    {
      const top = (await page.locator(".react-flow__node").first().boundingBox()).y;
      const dy = 125 - top;
      await page.mouse.move(600, 740);
      await page.mouse.down();
      await page.mouse.move(600, 740 + dy, { steps: 15 });
      await page.mouse.up();
      await page.waitForTimeout(500);
    }
    // One live window, parked at the right, over the map.
    await page.locator(".tr-overview .tr-ov-chip", { hasText: "Rate-limit the public" }).first().evaluate((el) => el.click());
    const w = page.locator("[data-session-window]").first();
    await w.waitFor();
    await page.waitForTimeout(800);
    const drag = async (loc, dx, dy) => {
      const b = await loc.boundingBox();
      const x = b.x + Math.min(40, b.width / 2), y = b.y + b.height / 2;
      await page.mouse.move(x, y);
      await page.mouse.down();
      await page.mouse.move(x + dx, y + dy, { steps: 12 });
      await page.mouse.up();
    };
    const layer = await page.locator("[data-window-layer]").boundingBox();
    let wb = await w.boundingBox();
    // Resize to ~46% of the layer width, ~70% of its height, then dock bottom-right.
    let bottom = 0;
    for (const n of await page.locator(".react-flow__node").all()) {
      const b = await n.boundingBox();
      if (b) bottom = Math.max(bottom, b.y + b.height);
    }
    const tw = layer.width * 0.62, th = layer.y + layer.height - 24 - (bottom + 28);
    await drag(w.locator("[data-window-resize]"), tw - wb.width, th - wb.height);
    wb = await w.boundingBox();
    await drag(w.locator("[data-window-head]"), layer.x + layer.width - 24 - (wb.x + wb.width), layer.y + layer.height - 24 - (wb.y + wb.height));
    await settle(page, 2500);
    await hideHints(page);
    await shoot(page, "map");
    report("map", unhandled);
    await ctx.close();
  },
  async "phone-dashboard"(browser) {
    const { ctx, page, unhandled } = await newPage(browser, PHONE);
    await page.goto("/");
    await page.getByRole("heading", { name: "Welcome, Commander." }).waitFor();
    await page.getByTestId("hq-primary-contact").waitFor();
    await settle(page, 2000);
    await shoot(page, "phone-dashboard");
    report("phone-dashboard", unhandled);
    await ctx.close();
  },
  async "phone-session"(browser) {
    const { ctx, page, unhandled } = await newPage(browser, PHONE);
    const s = F.SESSIONS[0];
    await page.goto(`/s/${s.engine}/${s.uuid}`);
    await page.locator(".xterm-rows").waitFor();
    await settle(page, 2500);
    await shoot(page, "phone-session");
    report("phone-session", unhandled);
    await ctx.close();
  },
  async "dashboard-light"(browser) {
    await SHOTS.dashboard(browser, "light", "dashboard-light");
  },
};

// A capture that misses a shot or meets a route the mock does not answer is a FAILED capture:
// the README, the tour and the landing reuse these files, so a silent partial run would ship
// stale or misleading images. Every failure is collected and the process exits non-zero.
const failures = [];
const written = new Set();

function report(name, unhandled) {
  if (unhandled.size) failures.push(`${name}: unhandled ${[...unhandled].sort().join(" ")}`);
}

const unknown = ONLY.filter((n) => !(n in SHOTS));
if (unknown.length) failures.push(`unknown shot(s): ${unknown.join(", ")}`);

const browser = await chromium.launch();
try {
  for (const [name, fn] of Object.entries(SHOTS)) {
    if (ONLY.length && !ONLY.includes(name)) continue;
    console.log(`shot ${name}`);
    try {
      await fn(browser);
    } catch (e) {
      failures.push(`${name}: ${String(e).slice(0, 600)}`);
    }
  }
} finally {
  await browser.close();
}

// The inventory: every requested shot (and its extras) must have produced a file in this run.
const EXPECTED = {
  files: ["files", "files-diff"],
  map: ["map", "map-overview"],
};
for (const name of Object.keys(SHOTS)) {
  if (ONLY.length && !ONLY.includes(name)) continue;
  for (const out of EXPECTED[name] ?? [name]) {
    if (!written.has(out)) failures.push(`${name}: no ${out}.png written`);
  }
}

if (failures.length) {
  console.error(`capture FAILED (${failures.length}):`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`capture OK — ${written.size} file(s) in ${OUT}`);
