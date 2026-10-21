import { expect, type Locator, type Page, test } from "@playwright/test";

/** The window's ONE chrome (#1109) — real-browser proof, desktop only.
 *
 *  jsdom cannot see what actually breaks here: which bars STACK inside a window (the #1109
 *  consolidation is a layout fact), what the measured fold really renders at a narrow window,
 *  whether the portalled chips land in the chrome bar at all, and whether the tint's
 *  `color-mix` resolves on a live surface. The unit table (`headActionsFold.test.ts`) pins the
 *  arithmetic; the unit wiring (`SessionWindowChrome.test.tsx`) pins the props; this spec pins
 *  the rendered result.
 */

const now = Math.floor(Date.now() / 1000);
const SESSIONS = [
  {
    id: "claude:s1",
    engine: "claude",
    uuid: "s1",
    short_uuid: "s1",
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    tag: "hot",
    last_mtime: now - 60,
    first_user_message: "",
    title: "Window session 1",
    sticky: false,
    archived: false,
    ai_summary: "",
  },
  // No tag: an untagged session must not render an empty tag chip in the chrome.
  {
    id: "claude:s2",
    engine: "claude",
    uuid: "s2",
    short_uuid: "s2",
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "proj", color: "#ffb000" },
    last_mtime: now - 120,
    first_user_message: "",
    title: "Window session 2",
    sticky: false,
    archived: false,
    ai_summary: "",
  },
  {
    id: "claude:s3",
    engine: "claude",
    uuid: "s3",
    short_uuid: "s3",
    cwd: "/home/u/other",
    project: { kind: "folder", id: "/home/u/other", name: "other" },
    last_mtime: now - 180,
    first_user_message: "",
    title: "Window session 3",
    sticky: false,
    archived: false,
    ai_summary: "",
  },
];

function payload(list: typeof SESSIONS) {
  return {
    sessions: list,
    next_offset: null,
    total: list.length,
    facets: { projects: [], engines: [] },
  };
}

async function mockApp(page: Page, visible: () => typeof SESSIONS = () => SESSIONS) {
  const projects = [{ id: "p1", name: "proj", color: "#ffb000", archived: false }];
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) => r.fulfill({ json: payload(visible()) }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[H${"window chrome spec\r\n".repeat(40)}`));
  });
}

const chip = (page: Page, n: number): Locator =>
  page.locator(".tr-overview .tr-ov-chip", { hasText: `Window session ${n}` });

const win = (page: Page, n: number): Locator =>
  page.locator(`[data-session-window="claude:s${n}"]`);

const head = (page: Page, n: number): Locator => win(page, n).locator("[data-window-head]");

async function openWindow(page: Page, n = 1) {
  await page.goto("/overview");
  // Folder clusters start collapsed (only project:p1 is expanded by config), and this spec
  // opens entity-less sessions too — expand everything so every chip is reachable. The
  // toolbar always carries the button, so no presence check: an unconditional click.
  await page.getByRole("button", { name: /expand all/i }).click();
  await expect(chip(page, n)).toBeVisible();
  await chip(page, n).click();
  await expect(win(page, n).locator(".xterm-screen")).toBeVisible();
}

test.describe("desktop", () => {
  // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
  test.beforeEach(async ({}, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "windows are desktop-only (#208)");
  });

  test("a window has ONE bar: the pane's own panelHead is gone, and the chrome carries its facts", async ({
    page,
  }) => {
    await mockApp(page);
    await openWindow(page);
    const w = win(page, 1);
    // The consolidation, stated on the layout: the window mounts exactly one head bar, and the
    // pane's own 26px `panelHead` is not among the bars inside it.
    await expect(w.locator("[data-window-head]")).toHaveCount(1);
    await expect(w.locator("[data-panel-head]")).toHaveCount(0);
    // The terminal reclaimed the head's height: the pane fills the window body, not the body
    // minus a second bar. (The compose bar still sits at the bottom; this is the top edge.)
    const headBox = await head(page, 1).boundingBox();
    const termBox = await w.locator(".xterm-screen").boundingBox();
    expect(headBox && termBox, "head and terminal laid out").toBeTruthy();
    expect(headBox!.y + headBox!.height).toBeLessThanOrEqual(termBox!.y + 1);

    // The chrome carries what the second bar used to render — through the SAME HeadFacts run:
    // the LED (an accessible image whose state names the link), the engine badge, the project
    // chip with its colour dot, the custom tag, the relative update time, and the title.
    const facts = w.locator("[data-window-facts]");
    await expect(facts.locator('[role="img"][data-head-led]')).toBeVisible();
    await expect(facts.locator('[title="claude"]')).toBeVisible();
    await expect(facts).toContainText("proj");
    await expect(facts.locator("[data-head-tag]")).toHaveText("hot");
    await expect(facts).toContainText(/(just now|a minute|ago|\d+m|\ds)/);
    await expect(head(page, 1)).toContainText("Window session 1");
  });

  test("an untagged session renders no tag chip; a folder project still shows its cwd chip", async ({
    page,
  }) => {
    await mockApp(page);
    await openWindow(page, 3);
    await expect(win(page, 3).locator("[data-head-tag]")).toHaveCount(0);
    // A folder project (no entity) has no colour to tint with: the bar keeps the plain ground
    // and the chip names the shortened cwd.
    await expect(win(page, 3)).not.toHaveAttribute("data-tinted", "true");
    await expect(win(page, 3).locator("[data-window-facts]")).toContainText("other");
  });

  test("the full-screen pane still renders its own single bar, untouched", async ({ page }) => {
    await mockApp(page);
    await page.goto("/s/claude/s1");
    const pane = page.locator("[data-panel-head]");
    await expect(pane).toBeVisible();
    // The pane's bar keeps every fact it shipped with (#1109 out of scope) — and no tag: the
    // full-screen surface is unchanged, only windows changed their chrome arrangement.
    await expect(pane).toContainText("proj");
    await expect(pane.locator("[data-head-tag]")).toHaveCount(0);
    await expect(page.locator("[data-window-head]")).toHaveCount(0);
  });

  test("the bar is tinted with the project's colour, and an entity-less project is not", async ({
    page,
  }) => {
    await mockApp(page);
    await openWindow(page);
    await expect(win(page, 1)).toHaveAttribute("data-tinted", "true");
    // The tint is driven by the inline --proj, computed the way the pane's project chip is.
    const proj = await win(page, 1).evaluate((el) =>
      (el as HTMLElement).style.getPropertyValue("--proj").trim(),
    );
    expect(proj).toBe("#ffb000");
    // And it really paints: the tinted ground differs from the plain chrome ground (s2's bar
    // would carry the same tint; s3's the plain one — compare s1 against s3).
    await chip(page, 3).click();
    await expect(win(page, 3).locator(".xterm-screen")).toBeVisible();
    const tinted = await head(page, 1).evaluate(
      (el) => getComputedStyle(el).backgroundColor,
    );
    const plain = await head(page, 3).evaluate(
      (el) => getComputedStyle(el).backgroundColor,
    );
    expect(tinted).not.toBe(plain);
  });

  test("one ⋯ per window: the chrome menu carries the session group and the folded pane group", async ({
    page,
  }) => {
    await mockApp(page);
    await openWindow(page);
    const w = win(page, 1);
    // Exactly ONE ⋯ in the window — the chrome's — and no fold trigger of the chips' own.
    await expect(w.locator("[data-window-menu]")).toHaveCount(1);
    await expect(w.locator('[data-testid="head-actions-menu"]')).toHaveCount(0);

    // At the default size the bar folds something (six chips + a facts run + a title do not
    // fit 720px), so the merged menu carries BOTH groups.
    await w.locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu).toBeVisible();
    // Labelled groups, session first, pane second — one rule between them, per the mockup.
    await expect(menu.locator("[data-menu-group='Session']")).toHaveText("Session");
    await expect(menu.locator("[data-menu-group='Pane']")).toHaveText("Pane");
    // The session group is the canonical list, unforked: the management run is there. Items
    // are named by their aria-labels (the labels ride the visible text), the shipped names.
    for (const name of [
      "Favorite session",
      "Rename session",
      "Edit session tag",
      "Move session to a project",
      "Archive session",
    ]) {
      await expect(menu.getByRole("menuitem", { name: new RegExp(`^${name}$`, "i") })).toBeVisible();
    }
    // The pane group carries the fold. Since #1329 the session-mirrored pane actions (Recap,
    // Hand off, mission) are menu-first, so at a width that folds anything they give way FIRST
    // into the Pane group — and the session group no longer names them.
    const paneItems = await menu
      .locator("[data-menu-group='Pane'] ~ [role='menuitem']")
      .allTextContents();
    expect(paneItems.join("|")).toMatch(/Repaint|text|Files|Recap|Hand off|mission/);
    // The session group never repeats a pane action: no "Session brief", "Hand off…", "Adopt to
    // mission…" while the pane side carries the same action.
    expect(paneItems.join("|")).not.toMatch(/Session brief|Hand off…|Adopt to mission…/);
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
  });

  test("a session-mirrored action is named ONCE — a chip or the ⋯ menu, never both (#1329)", async ({
    page,
  }) => {
    // The duplicate the operator saw: Recap / Hand off / Adopt to mission as chips on the bar AND
    // again in the ⋯ menu. Whatever the fold keeps on the bar is the chip; the menu must then drop
    // its session twin, so one action is named exactly once — chip or menu, never both.
    await mockApp(page);
    await openWindow(page);
    const w = win(page, 1);
    const chips = await w
      .locator("[data-head-action]")
      .evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));

    await w.locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu).toBeVisible();
    const items = await menu
      .locator("[role='menuitem']")
      .evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
    // No action's accessible name is on the bar AND in the menu at the same time.
    expect(chips.filter((n) => n && items.includes(n))).toEqual([]);
    // The session-mirrored actions are still reachable somewhere — chip or menu.
    const all = [...chips, ...items];
    expect(all).toContain("Open session brief");
    await page.keyboard.press("Escape");
  });

  test("a narrow window folds the text-size chips off the bar into that ONE menu", async ({
    page,
  }) => {
    await mockApp(page);
    await openWindow(page);
    // Shrink to the 560px floor via the keyboard grip — the fold must follow the real width.
    await win(page, 1).locator("[data-window-resize]").focus();
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    const slot = win(page, 1).locator("[data-window-actions-slot]");
    await expect(slot.locator("[data-head-action='text-bigger']")).toHaveCount(0);
    await expect(slot.locator("[data-head-action='files']")).toBeVisible(); // Files leads; folds last

    // ...and the folded pair is in the ⋯ menu, not gone.
    await win(page, 1).locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu.getByRole("menuitem", { name: "Bigger terminal text" })).toBeVisible();
    await expect(menu.getByRole("menuitem", { name: "Smaller terminal text" })).toBeVisible();
    await page.keyboard.press("Escape");
  });

  test("right-click on the bar opens the same ONE menu", async ({ page }) => {
    await mockApp(page);
    await openWindow(page);
    const h = await head(page, 1).boundingBox();
    expect(h).toBeTruthy();
    await page.mouse.click(h!.x + h!.width / 2, h!.y + h!.height / 2, { button: "right" });
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu).toBeVisible();
    await expect(menu.locator("[data-menu-group='Session']")).toHaveText("Session");
    await page.keyboard.press("Escape");
  });

  test("off the map, the ⋯ stays live and opens the PANE-ONLY menu; session actions withdraw", async ({
    page,
  }) => {
    let visible = SESSIONS;
    await mockApp(page, () => visible);
    await openWindow(page);
    // Narrow the window first, so the fold has actually parked chips in the overflow: at the
    // default width every chip fits and the pane group would be legitimately empty.
    await win(page, 1).locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    const btn = win(page, 1).locator("[data-window-menu]");
    await expect(btn).not.toHaveAttribute("aria-disabled", "true");

    // The session leaves the map (the shape a refetch that drops it takes). The window stays;
    // the ⋯ is NOT disabled — the pane actions never needed the map's row (Hermes on #1109).
    visible = SESSIONS.filter((s) => s.id !== "claude:s1");
    await page.getByRole("button", { name: /new project/i }).click();
    await page.getByLabel("Project name").fill("refetch");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(win(page, 1)).toBeVisible();
    await expect(btn).not.toHaveAttribute("aria-disabled", "true");
    await expect(btn).toHaveAttribute("title", /isn't on the map/);

    await btn.click();
    const menu = page.locator("[role='menu'][aria-label='Pane actions']");
    await expect(menu).toBeVisible();
    // No session group at all: the row-dependent actions are the ones that withdrew.
    expect(await menu.locator("[data-menu-group]").count()).toBe(0);
    // Whatever the fold parked (at this width the text-size pair) is what the menu carries —
    // a pane action, never a session one.
    await expect(
      menu
        .getByRole("menuitem", { name: /Repaint screen|terminal text|Browse session files/ })
        .first(),
    ).toBeVisible();
    // Session actions are NOT silently reachable from here.
    await expect(menu.getByRole("menuitem", { name: /Rename session/i })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
  });

  test("the merged menu never runs past the viewport, however low its window sits", async ({
    page,
  }) => {
    // Hermes on #1109, the P2: the Session+Pane merged menu is TALL, and near the bottom of
    // the workspace the old placement fallback put its tail — the only text-size controls —
    // below the screen with nothing visible to dismiss by. The menu is clamped and scrolls.
    await mockApp(page);
    await page.setViewportSize({ width: 1280, height: 720 });
    await openWindow(page);
    // Narrow the window so the fold actually parks the text-size pair in the Pane group.
    const w = page.locator("[data-session-window]");
    await w.locator("[data-window-resize]").focus();
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press("Shift+ArrowLeft");
    }
    // Push the window to the BOTTOM of the workspace: drag its bar to the lower edge.
    const bar = w.locator("[data-window-head]");
    const barBox = (await bar.boundingBox())!;
    await page.mouse.move(barBox.x + barBox.width / 2, barBox.y + barBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(640, 719, { steps: 8 });
    await page.mouse.up();
    const winBox = (await w.boundingBox())!;
    expect(winBox.y + winBox.height).toBeGreaterThan(600); // genuinely low on a 720px screen

    // Open the merged menu from the ⋯ ...
    await w.locator("[data-window-menu]").click();
    const menu = page.locator("[role='menu'][aria-label='Session actions']").last();
    await expect(menu).toBeVisible();
    await expect(menu.locator("[data-menu-group='Pane']")).toBeVisible();
    // ...and every entry of it is INSIDE the viewport: clamped placement, capped height.
    const box = (await menu.boundingBox())!;
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(720);
    // The text-size controls — the tail of the Pane group — are reachable: the menu scrolls,
    // and the item exists in the DOM inside the bounded menu.
    await expect(
      menu.getByRole("menuitem", { name: /terminal text/i }).first(),
    ).toBeAttached();
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
  });
});
