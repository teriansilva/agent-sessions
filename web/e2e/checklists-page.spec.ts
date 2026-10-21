/** Missions → Checklists as a Missions page (#1221), in a real browser.
 *
 *  What a jsdom test cannot see and this pins: the SHELL's sidebar lists missions on this route
 *  (docked on desktop, the drawer on a phone) from the console's rail and nothing else of the
 *  console; picking a mission or "+ New mission" is a route change the router's blocker holds
 *  while checklists are unsaved; the rail's scope is the Missions page's own; and the sticky save
 *  bar never covers the last control of a long checklist.
 */
import { expect, test, type Page } from "@playwright/test";

import { CHECKLISTS_PATH, MISSION_PATH } from "../src/lib/routes";
import {
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";
import { commonMocks, CONFIG, PLAYBOOKS } from "./mission-directions";

/** Ids in the server's shape: the console's deep link is shape-checked (`MISSION_ID_RE`) before
 *  it can select anything, so a toy id would be refused and prove nothing. */
const MSN_2 = `msn_${"2".repeat(32)}`;
const MSN_9 = `msn_${"9".repeat(32)}`;

/** A checklist long enough to scroll on either viewport, so the save bar has something to cover. */
const LONG = {
  ...PLAYBOOKS,
  playbooks: [
    {
      id: "ship",
      label: "Ship a change",
      objectives: Array.from({ length: 9 }, (_, k) => ({
        key: `step_${k + 1}`,
        title: `Step ${k + 1} is true`,
        probe: k % 2 ? "forge_pr" : "none",
        probe_args: null,
        gate: k % 2 === 1,
      })),
    },
    {
      id: "look",
      label: "Investigate",
      objectives: [
        {
          key: "finding",
          title: "A finding is written down",
          probe: "none",
          probe_args: null,
          gate: false,
        },
      ],
    },
  ],
};

async function setup(page: Page) {
  await commonMocks(page);
  // The spec's own playbooks, over the shared config (the LAST registered route answers first).
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: { ...CONFIG, mission_playbooks: LONG } }),
  );
  await mockMissions(page, {
    missions: (q: URLSearchParams) =>
      q.get("archived") === "1"
        ? missionList([
            missionRow({
              id: MSN_9,
              title: "An archived mission",
              archived_at: 1_700_000_100,
            }),
          ])
        : missionList([
            missionRow(),
            missionRow({ id: MSN_2, title: "Second mission" }),
          ]),
  });
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  const saves: unknown[] = [];
  await page.route("**/api/prefs", (r) => {
    saves.push(r.request().postDataJSON());
    return r.fulfill({ json: {} });
  });
  return { saves };
}

test("the sidebar lists MISSIONS here, and nothing else of the console mounts", async ({
  page,
}) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await expect(page.getByTestId("playbook-card").first()).toBeVisible();

  await openMissionRail(page);
  const rows = page.locator("aside.sidebar").getByTestId("rail-mission");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText("Kimi transcript adapter");
  await expect(
    page.locator("aside.sidebar").getByTestId("rail-new-mission"),
  ).toBeVisible();
  // Not the session list, and no console body, landing or composer on this page.
  await expect(page.getByTestId("sidebar-counts")).toHaveCount(0);
  await expect(page.getByTestId("mission-console")).toHaveCount(0);
  await expect(page.getByTestId("mission-foot-slot")).toBeAttached();
});

test("picking a mission opens it in the console", async ({ page }) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await openMissionRail(page);
  await page
    .locator("aside.sidebar")
    .getByTestId("rail-mission")
    .nth(1)
    .click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}$`));
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await expect(page.getByTestId("console-title")).toBeVisible();
});

test("+ New mission opens the console's new-mission page and closes the drawer", async ({
  page,
}) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await openMissionRail(page);
  await page.locator("aside.sidebar").getByTestId("rail-new-mission").click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}$`));
  await expect(page.getByTestId("mission-console")).toBeVisible();
  await expect(page.locator(".app.navOpen")).toHaveCount(0);
});

test("the rail's scope is the Missions page's scope", async ({ page }) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await openMissionRail(page);
  const aside = page.locator("aside.sidebar");
  await aside.getByTestId("rail-scope-archived").click();
  await expect(aside.getByTestId("rail-mission")).toHaveText([
    /An archived mission/,
  ]);
  // Archived has no "+ New mission", so leave through the top bar's Missions link.
  if ((await page.locator(".app.navOpen").count()) > 0) {
    await page.keyboard.press("Escape");
    await expect(page.locator(".app.navOpen")).toHaveCount(0);
  }
  await page.locator(`.hud-topbar a[href="${MISSION_PATH}"]`).first().click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}$`));
  await openMissionRail(page);
  await expect(
    page.locator("aside.sidebar").getByTestId("rail-scope-archived"),
  ).toHaveAttribute("aria-selected", "true");
});

test("leaving with unsaved checklists asks first: Keep editing keeps the draft, Discard leaves", async ({
  page,
}) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await page.getByTestId("playbook-card").first().click();
  await page.getByTestId("playbook-label").fill("Ship a change, carefully");
  await expect(page.getByTestId("playbook-dirty")).toBeVisible();

  await openMissionRail(page);
  await page
    .locator("aside.sidebar")
    .getByTestId("rail-mission")
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "Mission checklists" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Unsaved changes");

  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(page).toHaveURL(new RegExp(`${CHECKLISTS_PATH}$`));
  await expect(page.getByTestId("playbook-label")).toHaveValue(
    "Ship a change, carefully",
  );

  await openMissionRail(page);
  await page.locator("aside.sidebar").getByTestId("rail-new-mission").click();
  await page
    .getByRole("dialog", { name: "Mission checklists" })
    .getByRole("button", { name: "Discard and leave" })
    .click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}$`));
  await expect(page.getByTestId("mission-console")).toBeVisible();
});

test("switching between the list and a checklist never asks, and keeps the draft", async ({
  page,
}) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await page.getByTestId("playbook-card").nth(1).click();
  await page.getByTestId("playbook-label").fill("Investigate it");
  await page.getByTestId("playbook-back").click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("playbook-card").nth(1)).toContainText(
    "Investigate it",
  );
});

test("the last step's controls stay clear of the sticky save bar", async ({
  page,
}) => {
  const { saves } = await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await page.getByTestId("playbook-card").first().click();
  const heads = page.getByTestId("objective-toggle");
  await expect(heads).toHaveCount(9);
  // Expand the LAST step and make the page dirty, so the bar is at its tallest.
  await heads.last().click();
  await page.getByTestId("objective-title").fill("Step 9 is really true");
  await page.getByTestId("objective-add").scrollIntoViewIfNeeded();
  await page.evaluate(() => {
    const s = document.querySelector('[data-testid="checklists-page"]');
    if (s) s.scrollTop = s.scrollHeight;
  });
  const bar = await page
    .getByTestId("playbook-save")
    .evaluate((b) => b.parentElement!.getBoundingClientRect().top);
  for (const id of ["objective-remove", "objective-add"]) {
    const box = await page.getByTestId(id).last().boundingBox();
    expect(box, id).not.toBeNull();
    expect(box!.y + box!.height, `${id} bottom vs bar top`).toBeLessThanOrEqual(
      bar + 1,
    );
  }
  // …and the add is actually reachable: a click lands and opens the new step.
  await page.getByTestId("objective-add").click();
  await expect(heads).toHaveCount(10);
  await expect(heads.last()).toHaveAttribute("aria-expanded", "true");
  await page.getByTestId("playbook-save").click();
  await expect.poll(() => saves.length).toBe(1);
});

test("a step expands from the keyboard", async ({ page }) => {
  await setup(page);
  await page.goto(CHECKLISTS_PATH);
  await page.getByTestId("playbook-card").first().focus();
  await page.keyboard.press("Enter");
  const head = page.getByTestId("objective-toggle").first();
  await head.focus();
  await page.keyboard.press("Enter");
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await expect(head).toBeFocused();
  await expect(page.getByTestId("objective-title")).toBeVisible();
});
