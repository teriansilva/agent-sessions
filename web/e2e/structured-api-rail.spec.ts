import { expect, test, type Locator, type Page } from "@playwright/test";

import { animationsSettled, storeMotionChoice } from "./nightjarShell";
import { ID, mockStructuredGuidance } from "./structuredGuidanceFixture";
import { TOUCH_FLOOR } from "./touchTarget";

// #1726: the API pane's plan & tools rail. It docks beside the transcript when the MEASURED pane
// can spare the column (filePanelLayout), else it is a sheet over the pane; it shares the dock
// slot with Files; its body scrolls on its own; in a map window its toggle is part of the one
// chrome (#1109). Desktop + mobile, both themes, Full / Reduced / Off.

const NOW = Math.floor(Date.now() / 1000);
const PLAN = {
  entries: [
    { text: "Reproduce the gap with a 120-session fixture", status: "completed" },
    { text: "Filter the full set before paging", status: "completed" },
    { text: "Keep facets computed before filters", status: "in_progress" },
    { text: "Add the snapshot-pin regression test", status: "pending" },
    { text: "Update session-list-filtering.md", status: "pending" },
  ],
  explanation: "Paging dropped rows because the filter ran per page. Fix the order first.",
  entries_at: NOW - 60,
  document: "# Fix paging\n\n1. Filter first\n2. Then page\n",
  document_at: NOW - 120,
};
const ENVIRONMENT = {
  skill_use: true,
  mcp: [
    { name: "battlelab_memory", status: "connected" },
    { name: "forgejo", status: "connected" },
    { name: "nextcloud", status: "failed" },
    { name: "github", status: "configured" },
  ],
  skills: [{ name: "code-review" }, { name: "simplify" }, { name: "dataviz" }],
  commands: ["compact", "review", "init", "context"].map((name) => ({ name })),
};
const COUNTS = {
  tools: [
    { name: "Read", calls: 22 },
    { name: "Bash", calls: 14 },
    { name: "Edit", calls: 6 },
  ],
  tools_other: 0,
  servers: [
    { name: "forgejo", calls: 11 },
    { name: "battlelab_memory", calls: 4 },
  ],
  servers_other: 0,
  unattributed: 1,
  skills: [{ name: "code-review", calls: 1 }],
  skills_other: 0,
};

type Native = Record<string, unknown>;

async function session(
  page: Page,
  native: Native = {},
  { theme = "dark", motion = "full" }: { theme?: string; motion?: "full" | "reduced" | "off" } = {},
) {
  await storeMotionChoice(page, motion);
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt", theme);
  fixture.state.state = "idle";
  fixture.state.active_turn = "";
  fixture.state.turns[0].state = "completed";
  fixture.state.turns = [fixture.state.turns[0]];
  Object.assign(fixture.state.native, { plan: PLAN, environment: ENVIRONMENT, counts: COUNTS, ...native });
  return fixture;
}

const rail = (scope: Page | Locator) => scope.getByTestId("api-rail");

/** Files, the same way: a chip when it fits, else in the head's one menu. */
async function toggleFiles(page: Page) {
  const chip = page.locator("[data-head-action='files']");
  if (await chip.isVisible()) return chip.click();
  await page.getByRole("button", { name: /^(Actions for this session|More session actions)$/ }).first().click();
  await page.getByRole("menuitem", { name: "Browse session files" }).click();
}

/** The rail's toggle is a head action: a chip when it fits, else in the head's one menu. */
async function toggleRail(page: Page, scope: Page | Locator = page) {
  const chip = scope.locator("[data-head-action='plan-tools']");
  if (await chip.isVisible()) {
    await chip.click();
    return chip;
  }
  const menu = page.getByRole("button", { name: /^(Actions for this session|More session actions)$/ });
  await menu.first().click();
  await page.getByRole("menuitem", { name: /plan and tools/i }).click();
  return menu.first();
}

for (const theme of ["dark", "light"] as const) {
  test(`#1726: the rail docks beside a wide pane, a sheet over a narrow one (${theme})`, async ({ page }, testInfo) => {
    await session(page, {}, { theme });
    await page.goto(`/s/claude-api/${ID}`);
    await expect(page.getByTestId("structured-pane")).toBeVisible();
    await expect(rail(page)).toHaveCount(0); // closed until asked
    await toggleRail(page);
    await expect(rail(page)).toBeVisible();
    await animationsSettled(page); // a sheet slides up: measure where it lands
    const paneBox = (await page.getByTestId("structured-pane").boundingBox())!;
    // filePanelLayout: dock when the pane can spare MIN_W (260) beside its 360 px minimum.
    const expected = paneBox.width - 360 >= 260 ? "dock" : "sheet";
    await expect(rail(page)).toHaveAttribute("data-mode", expected);
    const railBox = (await rail(page).boundingBox())!;
    if (expected === "dock") {
      expect(railBox.width).toBeCloseTo(Math.min(340, paneBox.width - 360), 0);
      // The transcript and the composer sit beside it, never under it.
      const log = (await page.getByTestId("api-log").boundingBox())!;
      const form = (await page.getByRole("form", { name: "Compose message" }).boundingBox())!;
      expect(log.x + log.width).toBeLessThanOrEqual(railBox.x + 1);
      expect(form.x + form.width).toBeLessThanOrEqual(railBox.x + 1);
      await expect(page.getByRole("textbox").first()).toBeVisible();
    } else {
      await expect(page.getByRole("dialog", { name: "Plan and tools" })).toBeVisible();
      await expect(page.getByRole("tab", { name: /^Plan/ })).toBeFocused();
    }
    // Plan: every row has its word; the document is a closed disclosure.
    const rows = rail(page).getByTestId("api-rail-plan-entry");
    await expect(rows).toHaveCount(5);
    await expect(rows.nth(2)).toContainText("In progress");
    await expect(rows.nth(0)).toContainText("Done");
    await expect(page.getByRole("tab", { name: "Plan 2/5" })).toBeVisible();
    for (const target of [page.getByRole("tab", { name: "Plan 2/5" }), rail(page).getByRole("button", { name: /close plan and tools/i })]) {
      expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
    await page.screenshot({ path: testInfo.outputPath(`rail-plan-${expected}-${theme}.png`) });
    // Tools, by keyboard on the tab list.
    await page.getByRole("tab", { name: "Plan 2/5" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: /^Tools/ })).toHaveAttribute("aria-selected", "true");
    const servers = rail(page).getByTestId("api-rail-mcp-server");
    await expect(servers).toHaveCount(4);
    await expect(servers.nth(3)).toContainText("Configured");
    await expect(servers.nth(3).locator("[class*='led']")).toHaveCount(0);
    await expect(servers.nth(1)).toContainText("Connected");
    await expect(rail(page).getByTestId("api-rail-skill").first()).toContainText("Used this session");
    await page.screenshot({ path: testInfo.outputPath(`rail-tools-${expected}-${theme}.png`) });
  });
}

test("#1726: a sheet is modal over its pane — Escape closes it and focus goes back to the toggle", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "the phone gets the sheet");
  await session(page);
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  const opener = await toggleRail(page);
  const sheet = page.getByRole("dialog", { name: "Plan and tools" });
  await expect(sheet).toBeVisible();
  await animationsSettled(page);
  // Inside the pane, below the app bar; it never runs off the screen.
  const viewport = page.viewportSize()!;
  const box = (await sheet.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 1);
  // Tab stays inside the sheet.
  for (let i = 0; i < 8; i++) await page.keyboard.press("Tab");
  expect(await sheet.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test("#1726: Files and the rail share one dock slot — opening one closes the other", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "a dock needs the desktop pane");
  await session(page);
  await page.route("**/api/files/**", (r) => r.fulfill({ json: { ok: true, entries: [], path: "/home/u/demo", parent: null } }));
  // Files needs the session's folder from its row.
  const row = { id: `claude-api:${ID}`, engine: "claude-api", uuid: ID, short_uuid: ID.slice(0, 8),
    cwd: "/home/u/demo", project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: NOW, first_user_message: "Fix paging",
    title: "Fix paging", sticky: false, archived: false, ai_summary: "" };
  await page.route("**/api/sessions?**", (r) => r.fulfill({ json: {
    sessions: [row], next_offset: null, total: 1, facets: { projects: [], engines: [] },
  } }));
  await page.route(new RegExp(`/api/sessions/claude-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: row }));
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await toggleRail(page);
  await expect(rail(page)).toHaveAttribute("data-mode", "dock");
  await toggleFiles(page);
  await expect(rail(page)).toHaveCount(0);
  await expect(page.locator("[data-file-panel]").first()).toBeVisible();
  await toggleRail(page);
  await expect(rail(page)).toBeVisible();
  await expect(page.locator("[data-file-panel]")).toHaveCount(0);
});

test("#1726: empty and unavailable states are said, not shown as zeros", async ({ page }) => {
  await session(page, { plan: null, environment: null, counts: undefined, worker: "" });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await toggleRail(page);
  await expect(rail(page).getByTestId("api-rail-empty")).toContainText("No plan yet");
  await page.getByRole("tab", { name: /^Tools/ }).click();
  await expect(rail(page).getByTestId("api-rail-empty")).toContainText("Not running");
  await expect(rail(page).getByTestId("api-rail-tool-counts")).toContainText("No tool calls yet");
});

test("#1726: a long plan and 256 skills scroll inside the rail; the composer stays reachable", async ({ page }, testInfo) => {
  const entries = Array.from({ length: 100 }, (_, i) => ({ text: `Step ${i + 1}: do the thing number ${i + 1}`, status: i < 40 ? "completed" : i === 40 ? "in_progress" : "pending" }));
  const skills = Array.from({ length: 256 }, (_, i) => ({ name: `skill-${String(i).padStart(3, "0")}` }));
  const commands = Array.from({ length: 256 }, (_, i) => ({ name: `cmd-${i}` }));
  await session(page, {
    plan: { ...PLAN, entries },
    environment: { ...ENVIRONMENT, skills, commands },
    counts: { ...COUNTS, skills: [{ name: "skill-007", calls: 2 }] },
  });
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await toggleRail(page);
  const body = rail(page).getByTestId("api-rail-body");
  await expect(rail(page).getByTestId("api-rail-plan-entry")).toHaveCount(100);
  await animationsSettled(page);
  // The rail itself fits its pane; its body is the scroller.
  const pane = (await page.getByTestId("structured-pane").boundingBox())!;
  const box = (await rail(page).boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(pane.y + pane.height + 1);
  expect(await body.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  // Keyboard: the body is a scroll stop; End reaches the last step.
  await body.focus();
  await page.keyboard.press("End");
  await expect(rail(page).getByTestId("api-rail-plan-entry").last()).toBeInViewport();
  // Pointer / touch: a wheel over the body scrolls the body, not the page or the transcript.
  await body.evaluate((el) => el.scrollTo(0, 0));
  await body.hover();
  await page.mouse.wheel(0, 600);
  await expect.poll(() => body.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  await page.getByRole("tab", { name: /^Plan/ }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(rail(page).getByTestId("api-rail-skill")).toHaveCount(256);
  await body.focus();
  await page.keyboard.press("End");
  await expect(rail(page).getByTestId("api-rail-tool-counts")).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("rail-long-lists.png") });
  if ((await rail(page).getAttribute("data-mode")) === "dock") {
    await expect(page.getByRole("textbox").first()).toBeInViewport();
  } else {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("textbox").first()).toBeInViewport();
  }
});

for (const motion of ["full", "reduced", "off"] as const) {
  test(`#1726: the rail sheet's motion — ${motion}`, async ({ page }, info) => {
    test.skip(info.project.name !== "mobile", "the sheet is the phone's");
    await session(page, {}, { motion });
    await page.goto(`/s/claude-api/${ID}`);
    await expect(page.getByTestId("structured-pane")).toBeVisible();
    await toggleRail(page);
    const sheet = rail(page);
    await expect(sheet).toBeVisible();
    const anim = await sheet.evaluate((el) => {
      const s = getComputedStyle(el);
      return { name: s.animationName, duration: parseFloat(s.animationDuration) };
    });
    if (motion === "full") {
      expect(anim.name).toContain("nj-sheet-up");
      expect(anim.duration).toBeCloseTo(0.18, 2); // ≤ 180 ms interaction motion
    } else if (motion === "reduced") {
      expect(anim.name).toContain("nj-fade-in");
      expect(anim.duration).toBeLessThanOrEqual(0.12);
    } else {
      expect(anim.name === "none" || anim.duration === 0).toBe(true);
    }
  });
}

// ------------------------------------------------------------------------------- map window

async function openWindow(page: Page, theme = "dark", native: Native = {}) {
  await session(page, native, { theme });
  const row = { id: `claude-api:${ID}`, engine: "claude-api", uuid: ID, short_uuid: ID.slice(0, 8),
    cwd: "/home/u/demo", project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: NOW, first_user_message: "Fix paging", title: "Fix paging drops rows",
    sticky: false, archived: false, ai_summary: "" };
  await page.route("**/api/sessions?**", (r) => r.fulfill({ json: {
    sessions: [row], next_offset: null, total: 1, facets: { projects: [], engines: [] },
  } }));
  await page.route(new RegExp(`/api/sessions/claude-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: row }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "x", new_session_engines: ["claude-api"], terminal_backend: "ws", auth_mode: "none",
    default_project: "/home/u/demo", overview_expanded: ["project:p1"], projects_hidden: [], project_names: {}, theme,
  } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: {
    projects: [{ id: "p1", name: "proj", color: "#ffb000", archived: false }],
  } }));
  await page.goto("/overview");
  await page.locator(".tr-ov-chip").first().click();
  const win = page.locator(`[data-session-window="claude-api:${ID}"]`);
  await expect(win.getByTestId("structured-pane")).toBeVisible();
  return win;
}

async function toggleInWindow(page: Page, win: Locator) {
  const chip = win.locator("[data-head-action='plan-tools']");
  if (await chip.isVisible()) return chip.click();
  await win.locator("[data-window-menu]").click();
  const item = page.getByRole("menuitem", { name: /plan and tools/i });
  await expect(item).toHaveCount(1); // named once, in the one merged menu
  await item.click();
}

for (const theme of ["dark", "light"] as const) {
  test(`#1726: in a map window the rail follows the window body's width and its toggle is in the one chrome (${theme})`, async ({ page }, info) => {
    test.skip(info.project.name !== "desktop", "map windows are desktop-only");
    const win = await openWindow(page, theme);
    await expect(win.locator("[data-panel-head]")).toHaveCount(0); // the chrome is the only bar
    await toggleInWindow(page, win);
    const pane = (await win.getByTestId("structured-pane").boundingBox())!;
    const mode = pane.width - 360 >= 260 ? "dock" : "sheet";
    await expect(rail(win)).toHaveAttribute("data-mode", mode);
    const box = (await rail(win)!.boundingBox())!;
    const winBox = (await win.boundingBox())!;
    // Inside its window, never over the workspace.
    expect(box.x).toBeGreaterThanOrEqual(winBox.x - 1);
    expect(box.x + box.width).toBeLessThanOrEqual(winBox.x + winBox.width + 1);
    expect(box.y + box.height).toBeLessThanOrEqual(winBox.y + winBox.height + 1);
    if (mode === "dock") expect(box.width).toBeCloseTo(Math.min(340, pane.width - 360), 0);
    await page.screenshot({ path: info.outputPath(`rail-window-${mode}-${theme}.png`) });
    // The merged menu marks it shown.
    await win.locator("[data-window-menu]").click();
    const item = page.getByRole("menuitem", { name: /plan and tools/i });
    if (await item.count()) await expect(item).toContainText("Shown");
    await page.keyboard.press("Escape");
  });
}

test("#1726: a short map window keeps the composer, the strip and the rail reachable", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "map windows are desktop-only");
  const entries = Array.from({ length: 60 }, (_, i) => ({ text: `Step ${i + 1}`, status: "pending" }));
  const jobs = Array.from({ length: 20 }, (_, i) => ({ task_id: `t${i}`, state: "running",
    description: `job ${i}`, started_at: NOW - 60, updated_at: NOW - 5 }));
  const win = await openWindow(page, "dark", {
    plan: { ...PLAN, entries }, background_active: true, background_tasks: jobs,
  });
  // A short window: the keyboard grip steps the height down.
  await win.locator("[data-window-resize]").focus();
  for (let i = 0; i < 8; i++) await page.keyboard.press("Shift+ArrowUp");
  const pane = (await win.getByTestId("structured-pane").boundingBox())!;
  // The strip opens into a capped list; the composer stays inside the window and visible.
  const strip = win.getByTestId("structured-background-work");
  await strip.getByTestId("structured-background-strip").locator("> button").click();
  const list = (await win.getByTestId("structured-background-list").boundingBox())!;
  expect(list.height).toBeLessThanOrEqual(pane.height * 0.4 + 2);
  const winBox = (await win.boundingBox())!;
  const form = (await win.getByRole("form", { name: "Compose message" }).boundingBox())!;
  expect(form.y + form.height).toBeLessThanOrEqual(winBox.y + winBox.height + 1);
  await expect(win.getByRole("textbox").first()).toBeVisible();
  // The rail fits the short window and scrolls its long plan by keyboard.
  await toggleInWindow(page, win);
  await expect(rail(win)).toBeVisible();
  const box = (await rail(win).boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(winBox.y + winBox.height + 1);
  const body = rail(win).getByTestId("api-rail-body");
  await body.focus();
  await page.keyboard.press("End");
  await expect(rail(win).getByTestId("api-rail-plan-entry").last()).toBeInViewport();
  await page.screenshot({ path: info.outputPath("rail-short-window.png") });
});

test("#1726: Shift+Tab from the selected Tools tab stays inside the sheet; the pane behind is inert (Hermes on #1811)", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "the phone gets the sheet");
  await session(page);
  await page.goto(`/s/claude-api/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await toggleRail(page);
  const sheet = page.getByRole("dialog", { name: "Plan and tools" });
  await expect(sheet).toBeVisible();
  await page.getByRole("tab", { name: /^Plan/ }).focus();
  await page.keyboard.press("ArrowRight");
  const tools = page.getByRole("tab", { name: /^Tools/ });
  await expect(tools).toBeFocused();
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press("Shift+Tab");
    expect(await sheet.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  }
  // Behind the modal sheet nothing takes focus or a pointer: the composer is inert.
  expect(await page.getByRole("textbox").first().evaluate((el) => !!el.closest("[inert]"))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  expect(await page.getByRole("textbox").first().evaluate((el) => !!el.closest("[inert]"))).toBe(false);
});
