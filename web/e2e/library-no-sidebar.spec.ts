import { expect, test, type Page } from "@playwright/test";

import { AUTOMATIONS_PATH, NEW_SESSION_PATH, SKILLS_PATH, WEBHOOKS_PATH } from "../src/lib/routes";
import { mockAutomations } from "./automations";

/** Skills and Automations render WITHOUT a sidebar, like the rest of the Library (operator,
 *  2026-11-19: "Automation has a missions sidebar and Skills has a sessions sidebar. The library
 *  should not have the sidebar").
 *
 *  Same contract as Settings (`settings-no-sidebar.spec.ts`, #1129): the shell's `<aside>` stays
 *  mounted but is `display: none`, and the toggle stands down with it, at both widths. Skills
 *  showed the SESSION list; Automations showed the MISSION rail (it sits under `/mission`).
 *  Real-browser assertions: a hidden panel vs an off-canvas drawer is a layout fact. */

async function mock(page: Page) {
  await mockAutomations(page);
  await page.route("**/api/library/skills", (r) => r.fulfill({ json: { skills: [] } }));
}

const ROUTES: [string, string][] = [
  ["Skills", SKILLS_PATH],
  ["Automations", AUTOMATIONS_PATH],
  ["an automation's runs", `${AUTOMATIONS_PATH}/a1`],
  // #2221: Webhooks, and its subtree.
  ["Webhooks", WEBHOOKS_PATH],
  ["a new webhook", `${WEBHOOKS_PATH}/new`],
];

for (const [name, path] of ROUTES) {
  test(`${name} has no sidebar and no sidebar toggle`, async ({ page }) => {
    await mock(page);
    await page.goto(path);
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(page.locator("aside.sidebar")).toBeHidden();
    await expect(page.locator("header .navToggle")).toHaveCount(0);
  });
}

test("a session route still has its sidebar — the removal is Library-specific", async ({ page }) => {
  await mock(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(page.locator("header .navToggle")).toHaveCount(1);
  if ((await page.locator("aside.sidebar").isHidden()) && (await page.locator("header .navToggle").count())) {
    // On a phone the session list is the off-canvas drawer: open it to see it.
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
});
