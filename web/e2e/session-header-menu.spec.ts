/** On a phone the session pane header is ONE Actions menu (#948 P6).
 *
 * At the shell's ≤800px breakpoint every header action moves into a single labelled menu, each with
 * its full label. This deliberately replaces #744/#859's "every action one tap away on touch" on
 * phones only; wider panes keep the measured fold. The menu reuses HeadActions' contract: roving
 * menuitems, arrow keys, Escape, and focus back on the trigger.
 */
import { expect, test } from "@playwright/test";
import { setupBench } from "./terminal/harness";

const ENGINE = "claude";
const UUID = "ffffffff-1111-2222-3333-444444444444";
const TITLE = "Tidy the compose bar";

test.beforeEach(async ({ page }) => {
  await setupBench(page, { sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }] });
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: `${ENGINE}:${UUID}`,
            engine: ENGINE,
            uuid: UUID,
            short_uuid: "ffffffff",
            cwd: "/home/u/proj",
            project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
            last_mtime: 1_700_000_000,
            first_user_message: "",
            title: TITLE,
            sticky: false,
            archived: false,
            mission: null,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: 1 },
      },
    }),
  );
});

test("≤800px: one Actions trigger, every action inside it with its label", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "phone-width contract");
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const head = page.locator('[class*="panelHead"]');
  const trigger = head.getByRole("button", { name: "Actions for this session" });
  await expect(trigger).toBeVisible();
  await expect(trigger).toContainText("Actions");
  const box = (await trigger.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(44);
  expect(box.width).toBeGreaterThanOrEqual(44);

  // No action chip is left on the bar.
  for (const name of [/open session brief/i, /hand off/i, /smaller terminal text/i, /bigger terminal text/i]) {
    await expect(head.getByRole("button", { name })).toHaveCount(0);
  }

  await trigger.click();
  const menu = page.getByRole("menu", { name: "Actions for this session" });
  await expect(menu).toBeVisible();
  for (const name of [
    /open session brief/i,
    /hand off/i,
    /repaint/i,
    /smaller terminal text/i,
    /bigger terminal text/i,
    /adopt this session into a mission/i,
  ]) {
    await expect(menu.getByRole("menuitem", { name })).toBeVisible();
  }
  // Labels travel with the actions — the menu is not an icon grid.
  await expect(menu.getByRole("menuitem", { name: /open session brief/i })).toContainText("Recap");

  // Keyboard: focus is inside the menu, arrows move it, Escape closes and returns to the trigger.
  const focusedName = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? "");
  const first = await focusedName();
  await page.keyboard.press("ArrowDown");
  await expect.poll(focusedName).not.toBe(first);
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();

  // A menu item is a real control: it opens the session brief.
  await trigger.click();
  await page.getByRole("menuitem", { name: /open session brief/i }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("≤800px on a SHORT screen: the menu fits the viewport and its last action can be tapped", async ({
  page,
}, testInfo) => {
  // #958 review: at 740×360 (a landscape phone) the all-actions menu ran past the bottom of the
  // viewport, and a real tap on its last item failed "element is outside of the viewport".
  // toBeVisible() cannot see that; tapping the LAST action and checking its effect can.
  test.skip(testInfo.project.name !== "mobile", "touch contract");
  await page.setViewportSize({ width: 740, height: 360 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const trigger = page.locator('[class*="panelHead"]').getByRole("button", { name: "Actions for this session" });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Actions for this session" });
  await expect(menu).toBeVisible();
  const box = (await menu.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(360);

  const size = () => page.evaluate(() => localStorage.getItem("tr-termsize"));
  const before = await size();
  const last = menu.getByRole("menuitem", { name: /bigger terminal text/i });
  await last.scrollIntoViewIfNeeded();
  await last.tap();
  await expect.poll(size).not.toBe(before);
});

test("≤800px on a SHORT screen: arrow keys reach the last action, and scrolling does not steal focus", async ({
  page,
}, testInfo) => {
  // #958 review 4810: the capped menu scrolls, every scroll re-placed it, and a new position
  // re-ran "focus the first item" — so ArrowDown to an offscreen item snapped focus back to Files
  // and the last action was unreachable by keyboard. Walk to it, let scrolling settle, and USE it.
  test.skip(testInfo.project.name !== "mobile", "phone-width contract");
  await page.setViewportSize({ width: 740, height: 360 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const trigger = page.locator('[class*="panelHead"]').getByRole("button", { name: "Actions for this session" });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Actions for this session" });
  await expect(menu).toBeVisible();
  const last = menu.getByRole("menuitem", { name: /bigger terminal text/i });
  const focused = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? "");

  for (let i = 0; i < 12 && !/bigger terminal text/i.test(await focused()); i++) {
    const before = await focused();
    await page.keyboard.press("ArrowDown");
    await expect.poll(focused).not.toBe(before);
  }
  await expect(last).toBeFocused();
  // Let every scroll the walk caused land; focus must still be on the last action.
  await page.waitForTimeout(400);
  await expect(last).toBeFocused();

  const size = () => page.evaluate(() => localStorage.getItem("tr-termsize"));
  const before = await size();
  await page.keyboard.press("Enter");
  await expect.poll(size).not.toBe(before);
});

test(">800px: the chips stay on the bar and there is no Actions trigger", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "desktop contract");
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const head = page.locator('[class*="panelHead"]');
  await expect(head.getByRole("button", { name: /open session brief/i })).toBeVisible();
  await expect(head.getByRole("button", { name: "Actions for this session" })).toHaveCount(0);
});

test("≤800px: adopting from the Actions menu returns focus to the Actions trigger", async ({ page }, testInfo) => {
  // The desktop header test (#953) pins focus on the relabelled chip. On a phone the opener is the
  // menu's own trigger, which survives both the menu closing and the Adopt → Open mission flip.
  test.skip(testInfo.project.name !== "mobile", "phone-width contract");
  const MISSION = "msn_" + "a".repeat(32);
  let held: { id: string; title: string; state: string } | null = null;
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: `${ENGINE}:${UUID}`,
            engine: ENGINE,
            uuid: UUID,
            short_uuid: "ffffffff",
            cwd: "/home/u/proj",
            project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
            last_mtime: 1_700_000_000,
            first_user_message: "",
            title: TITLE,
            sticky: false,
            archived: false,
            mission: held,
          },
        ],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: [ENGINE], missions: [], no_mission: held ? 0 : 1 },
      },
    }),
  );
  await page.route(/\/api\/missions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        missions: [
          {
            id: MISSION,
            title: "Harden the upload retry path",
            project_id: null,
            cwd: "/repo",
            state: "running",
            created_at: 1_700_000_000,
            updated_at: 1_700_000_000,
            closed_at: null,
            archived_at: null,
            outcome: null,
            session_keys: [],
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
        facets: { projects: [], states: [] },
        store_error: null,
        snapshot: "s",
      },
    }),
  );
  await page.route(/\/api\/missions\/msn_[0-9a-f]{32}\/adopt$/, (r) => {
    held = { id: MISSION, title: "Harden the upload retry path", state: "running" };
    return r.fulfill({ json: { id: MISSION } });
  });

  await page.goto(`/s/${ENGINE}/${UUID}`);
  const trigger = page.locator('[class*="panelHead"]').getByRole("button", { name: "Actions for this session" });
  await trigger.click();
  await page.getByRole("menuitem", { name: /adopt this session into a mission/i }).click();
  const dialog = page.getByRole("dialog", { name: "Adopt to mission" });
  await dialog.getByTestId("adopt-option").first().click();
  await dialog.getByTestId("adopt-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();

  await trigger.click();
  await expect(
    page.getByRole("menuitem", { name: "Open mission Harden the upload retry path" }),
  ).toBeVisible();
});
