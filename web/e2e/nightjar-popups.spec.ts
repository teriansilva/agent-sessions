/** NIGHTJAR popups in a real browser (#1531 Phase 3), desktop and mobile.
 *
 *  1. **Interactive from the first frame.** A menu appears with a fade (no rise, never a scale), and
 *     a press that lands DURING that animation must act — the animation is decoration, not a gate.
 *     The press is a real pointer event at the item's measured centre (hit-testing included), sent
 *     while the panel still has a running animation; Playwright's own `click()` would wait for the
 *     element to be "stable" and so prove nothing.
 *  2. **Every menu is a bottom sheet on a phone**: a scrim, the sheet pinned to the floor, Esc
 *     closes it and focus returns to the trigger.
 *
 *  The operator menu is the subject: it is in the top bar at every width. */
import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_PATH } from "../src/lib/routes";
import { mockNightjarShell, storeMotionChoice } from "./nightjarShell";

const trigger = (page: Page) => page.getByTestId("operator-menu");
const panel = (page: Page) => page.getByTestId("operator-menu-panel");

test.beforeEach(async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockNightjarShell(page);
  await storeMotionChoice(page, "full");
});

test("a menu item acts when pressed during the deploy-in animation (#1531)", async ({ page }) => {
  await page.goto(DASHBOARD_PATH);
  await trigger(page).click();
  // FREEZE the entrance part-way (a desktop menu's fade, a phone sheet's slide-up), then press the
  // item where it is drawn at that moment. Pausing makes the claim deterministic on any runner (a
  // loaded one used to finish the 180 ms fade first) and lets a sheet that is still MOVING be
  // pressed where it is, rather than where it was when measured.
  const hit = await panel(page).evaluate((el) => {
    const running = el.getAnimations({ subtree: false }).filter((a) => a.playState === "running");
    for (const a of running) a.pause();
    const item = el.querySelector<HTMLElement>('[role="menuitem"]')!;
    const r = item.getBoundingClientRect();
    return {
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      animating: running.length > 0,
      label: item.textContent?.trim() ?? "",
    };
  });
  await page.mouse.click(hit.x, hit.y);
  // The first item is Settings: the press navigated, and the menu closed behind it.
  await expect(page).toHaveURL(/\/settings/);
  await expect(panel(page)).toHaveCount(0);
  expect(hit.label).toContain("Settings");
  // …and the press really landed mid-animation: that is the claim this test exists for.
  expect(hit.animating).toBe(true);
});

test("on a phone the menu is a bottom sheet over a scrim; Esc closes it and focus returns (#1531)", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the sheet is the ≤800px presentation");
  await page.goto(DASHBOARD_PATH);
  await trigger(page).click();
  const sheet = panel(page);
  await expect(sheet).toBeVisible();
  await expect(sheet).toHaveAttribute("data-sheet", "");
  await expect(page.getByTestId("operator-menu-panel-scrim")).toBeVisible();

  // Pinned to the floor, full width.
  const vp = page.viewportSize()!;
  await expect
    .poll(async () => {
      const b = (await sheet.boundingBox())!;
      return [Math.round(b.x), Math.round(b.width), Math.round(b.y + b.height)];
    })
    .toEqual([0, vp.width, vp.height]);
  // The first item is focused, as on desktop.
  await expect(sheet.getByRole("menuitem").first()).toBeFocused();
  // The page behind is inert while the sheet is up.
  await expect(page.locator("#root")).toHaveAttribute("inert", "");

  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(trigger(page)).toBeFocused();
  await expect(page.locator("#root")).not.toHaveAttribute("inert", "");
});

test("on a phone a press on the scrim and Cancel each close the sheet (#1531)", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "the sheet is the ≤800px presentation");
  await page.goto(DASHBOARD_PATH);

  await trigger(page).click();
  await expect(panel(page)).toBeVisible();
  // Above the sheet, on the scrim.
  await page.mouse.click(20, 40);
  await expect(panel(page)).toHaveCount(0);

  await trigger(page).click();
  await panel(page).getByRole("button", { name: "Cancel" }).click();
  await expect(panel(page)).toHaveCount(0);
  await expect(trigger(page)).toBeFocused();
});
