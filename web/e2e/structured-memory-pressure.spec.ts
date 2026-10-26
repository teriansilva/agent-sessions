import { TOUCH_FLOOR } from "./touchTarget";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";

// An API session near its memory limit (#1643) says so before the kill: a banner in its pane and a
// chip on its sidebar row, both from the server's reading of the live generation, both gone the
// moment that reading is. Red on origin/main: neither exists, so the operator learns of the limit
// only from the turn that died of it.
const ENGINE = "codex-api";
const KEY = `${ENGINE}:${ID}`;
const GIB = 1024 ** 3;
const HIGH = { level: "high", working_set: Math.round(6.4 * GIB), max: 8 * GIB };
// Critical with throttle evidence: this session's own limit was seen holding it back.
const CRITICAL = {
  level: "critical", working_set: Math.round(7.6 * GIB), max: 8 * GIB, throttled: "session",
};
const now = Math.floor(Date.now() / 1000);

const row = (memory_pressure: unknown, extra: Record<string, unknown> = {}) => ({
  id: KEY, engine: ENGINE, uuid: ID, short_uuid: ID.slice(0, 8), cwd: "/home/u/demo",
  project: { kind: "folder", id: "/home/u/demo", name: "demo" }, last_mtime: now - 60,
  first_user_message: "", title: "Implement the localization epic", sticky: false,
  archived: false, ai_summary: "", reviewed_at: null, review_excluded: false, has_draft: false,
  model_requested: null, model_effective: null, memory_pressure, ...extra,
});

async function setup(page: Page, theme: string, memory: unknown, extra: Record<string, unknown> = {}) {
  const { state } = await mockStructuredGuidance(page, ENGINE, "steer", theme);
  const server = { memory };
  Object.defineProperty(state.native, "memory_pressure", {
    get: () => server.memory, enumerable: true,
  });
  await page.route("**/api/sessions**", (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path !== "/api/sessions") return r.fulfill({ json: row(server.memory, extra) });
    return r.fulfill({ json: {
      sessions: [row(server.memory, extra)], next_offset: null, total: 1,
      facets: { projects: [], engines: [] },
    } });
  });
  return server;
}

// WCAG contrast of an element's text against the first opaque background up its tree.
async function contrast(l: Locator): Promise<number> {
  return l.evaluate((el) => {
    // `rgb(…)` channels are 0–255; a `color-mix()` token computes to `color(srgb …)`, 0–1.
    const rgb = (c: string) => {
      const n = (c.match(/[\d.]+/g) ?? []).map(Number);
      return c.startsWith("color(srgb") ? n.map((v, i) => (i < 3 ? v * 255 : v)) : n;
    };
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    let bg: number[] = [];
    for (let n: Element | null = el; n; n = n.parentElement) {
      const c = rgb(getComputedStyle(n).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] === 1)) { bg = c.slice(0, 3); break; }
    }
    const fg = rgb(getComputedStyle(el).color).slice(0, 3);
    const [a, b] = [lum(fg), lum(bg.length ? bg : [255, 255, 255])].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  });
}

for (const theme of ["dark", "light"]) {
  test(`${theme}: a session near its limit says so in its pane and on its row`, async ({ page }, testInfo) => {
    await setup(page, theme, HIGH);
    await page.goto(`/s/${ENGINE}/${ID}`);
    const banner = page.getByTestId("structured-memory");
    await expect(banner).toHaveAttribute("data-level", "high");
    await expect(banner).toContainText("Memory high");
    await expect(banner).toContainText("6.4 of 8 GB in use by this session’s agent and the tools it started.");
    const action = banner.getByRole("button", { name: "Resource limits" });
    expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(await contrast(banner.locator("b"))).toBeGreaterThanOrEqual(4.5);
    expect(await contrast(banner.locator("p"))).toBeGreaterThanOrEqual(4.5);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    if (testInfo.project.name === "desktop") {
      const chip = page.getByTestId("row-memory-tag");
      await expect(chip.locator("[aria-hidden=true]").last()).toHaveText("HIGH 6.4/8 GB");
      await expect(chip).toContainText("Memory high: 6.4 of 8 GB");
      await expect(chip).toHaveAttribute("data-level", "high");
      expect(await contrast(chip)).toBeGreaterThanOrEqual(4.5);
    }
    await page.screenshot({ path: testInfo.outputPath(`memory-high-${theme}.png`) });
    await action.click();
    await expect(page).toHaveURL(/\/settings\/resources$/);
  });
}

test("critical survives a reload and clears when the reading does", async ({ page }, testInfo) => {
  const server = await setup(page, "dark", CRITICAL);
  await page.goto(`/s/${ENGINE}/${ID}`);
  const banner = page.getByTestId("structured-memory");
  await expect(banner).toHaveAttribute("data-level", "critical");
  await expect(banner).toContainText("7.6 of 8 GB — close to this session’s limit.");
  await expect(banner).toContainText("The kernel is holding this session back at its own limit");
  await page.reload();
  await expect(banner).toHaveAttribute("data-level", "critical");
  if (testInfo.project.name === "desktop") {
    await expect(page.getByTestId("row-memory-tag")).toHaveAttribute("data-level", "critical");
    await expect(page.getByTestId("row-memory-tag")).toContainText("CRIT 7.6/8 GB");
  }
  await page.screenshot({ path: testInfo.outputPath("memory-critical.png") });
  // The server stops sending it (recovered, unreadable, or the worker is gone): the pane's next
  // lifecycle re-read drops the banner without a reload.
  server.memory = null;
  await expect(banner).toHaveCount(0, { timeout: 15_000 });
  await page.reload();
  await expect(page.getByTestId("row-memory-tag")).toHaveCount(0);
});

// Hermes on #1651: the row's meta line clips its tail (`nowrap; overflow: hidden`), and a sidebar
// may be 220 px wide. With a requested model and a mission tag on the same line, the warning must
// still be inside the line's clip rect — it leads the line for exactly that reason.
test("the row's warning stays visible on the narrowest sidebar beside model and mission tags", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "the resizable sidebar is the desktop layout");
  await page.addInitScript(() => localStorage.setItem("tr-sidebar-w", "220"));
  await setup(page, "dark", CRITICAL, {
    model_requested: "gpt-6-codex-max-preview",
    mission: { id: "m-1", title: "Localize every platform surface", state: "running" },
  });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const chip = page.getByTestId("row-memory-tag");
  await expect(chip).toContainText("CRIT 7.6/8 GB");
  await expect(chip).toContainText("Memory critical: 7.6 of 8 GB");
  await expect(page.getByTestId("row-mission-tag")).toBeVisible();
  const box = (await chip.boundingBox())!;
  const clip = (await chip.locator("xpath=..").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(clip.x);
  expect(box.x + box.width).toBeLessThanOrEqual(clip.x + clip.width + 0.5);
  await page.screenshot({ path: testInfo.outputPath("memory-row-220.png") });
});

test("a parent group's limit is named as shared and offers no Resource limits shortcut", async ({ page }) => {
  await setup(page, "dark", { ...HIGH, scope: "shared" });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const banner = page.getByTestId("structured-memory");
  await expect(banner).toHaveAttribute("data-scope", "shared");
  await expect(banner).toContainText(
    "6.4 of 8 GB in a group this session shares with other work, limited outside BattleLab.",
  );
  await expect(banner).toContainText("Raising this session’s own limit won’t help.");
  await expect(banner.getByRole("button", { name: "Resource limits" })).toHaveCount(0);
});

// Hermes r6 on #1651: "the kernel is holding it back" only on evidence; a group merely close to
// its hard cap says so. And the session's own throttling never hides a more urgent shared group.
test("critical without throttle evidence says close to the limit, not held back", async ({ page }) => {
  await setup(page, "dark", { ...CRITICAL, throttled: null });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const banner = page.getByTestId("structured-memory");
  await expect(banner).toContainText("7.6 of 8 GB — close to this session’s limit.");
  await expect(banner).not.toContainText("holding");
});

test("a shared group near its cap is shown beside this session's own throttling", async ({ page }) => {
  await setup(page, "dark", {
    level: "critical", working_set: Math.round(9.5 * GIB), max: 10 * GIB, scope: "shared",
    throttled: "session",
  });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const banner = page.getByTestId("structured-memory");
  await expect(banner).toContainText("9.5 of 10 GB in a group this session shares — close to that group’s limit");
  await expect(banner).not.toContainText("A group this session shares is being held back");
  await expect(banner).toContainText("The kernel is holding this session back at its own limit");
  // Its own throttling is something Resource limits can help with.
  await expect(banner.getByRole("button", { name: "Resource limits" })).toBeVisible();
});

// Hermes r7 on #1651: this session's own ratio is the one shown (7 of 8 GB), but the only
// throttling observed is a shared group's. The copy must not blame this session's limit, and the
// Resource limits shortcut — which cannot lift a shared group's throttling — is not offered.
test("a shared group's throttling is never presented as this session's own", async ({ page }) => {
  await setup(page, "dark", {
    level: "critical", working_set: 7 * GIB, max: 8 * GIB, scope: "session", throttled: "shared",
  });
  await page.goto(`/s/${ENGINE}/${ID}`);
  const banner = page.getByTestId("structured-memory");
  await expect(banner).toContainText("7 of 8 GB in use by this session’s agent and the tools it started.");
  await expect(banner).toContainText("A group this session shares is being held back at its limit");
  await expect(banner).not.toContainText("holding this session back");
  await expect(banner.getByRole("button", { name: "Resource limits" })).toHaveCount(0);
});
