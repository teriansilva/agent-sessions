/** The per-mission opt-in to autonomous menu answers (#1060 Phase 4), in a real browser.
 *
 *  It lives in the mission's Follow-through section. At yolo it can be turned on, and the request is
 *  exactly `{auto_choose: true}` on this mission; below yolo it cannot be turned on and says why.
 *  An autonomous answer the server recorded appears in the thread with the option it chose.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionDetails,
  openMissionRail,
} from "./mission-console";

const T = 1_700_000_000;

function config(autonomy: string) {
  return {
    csrf: "x",
    new_session_engines: ["claude"],
    terminal_backend: "ws",
    auth_mode: "none",
    overview_expanded: [],
    projects_hidden: [],
    pulse: { configured: true },
    orchestrator: {
      enabled: true,
      autonomy,
      allowed_verbs: ["continue"],
      auto_verbs_ceiling: ["continue"],
    },
  };
}

async function open(
  page: Page,
  autonomy: string,
  over: Record<string, unknown> = {},
) {
  await page.route("**/api/config", (r) =>
    r.fulfill({ json: config(autonomy) }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
  const mission = {
    ...MISSION,
    state: "running",
    auto_choose: false,
    events: [],
    events_next_seq: null,
    ...over,
  };
  await mockMissions(page, {
    missions: missionList([missionRow({ state: "running" })]),
    mission,
  });
  const patches: { auto_choose: boolean }[] = [];
  // The mission detail reflects what was SAVED: the box follows the server, never its own click.
  await page.route(/\/api\/missions\/msn_1(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        ...mission,
        auto_choose: patches.at(-1)?.auto_choose ?? mission.auto_choose,
      },
    }),
  );
  await page.route("**/api/missions/*/autonomy", (r) => {
    const body = r.request().postDataJSON() as { auto_choose: boolean };
    patches.push(body);
    return r.fulfill({
      json: { id: MISSION.id, auto_choose: body.auto_choose },
    });
  });
  await page.goto("/mission");
  await openMissionRail(page);
  await page
    .getByRole("navigation", { name: /missions/i })
    .getByRole("button", { name: /Kimi transcript adapter/i })
    .first()
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await openMissionDetails(page, "followThrough");
  return { patches };
}

const box = (page: Page) =>
  page.locator('[data-testid="mission-auto-choose-toggle"]:visible');

test("at yolo the opt-in is in Follow-through and sends exactly {auto_choose: true}", async ({
  page,
}) => {
  const { patches } = await open(page, "yolo");
  await expect(box(page)).toBeVisible();
  await expect(box(page)).not.toBeChecked();
  await expect(
    page.locator('[data-testid="mission-auto-choose"]:visible'),
  ).toContainText("never a permission prompt");
  // A 44px target, like every control on this surface.
  const row = await page
    .locator('[data-testid="mission-auto-choose"]:visible label')
    .boundingBox();
  expect(row!.height).toBeGreaterThanOrEqual(44);
  await box(page).check();
  await expect.poll(() => patches).toEqual([{ auto_choose: true }]);
});

test("below yolo it cannot be turned on, and says why", async ({ page }) => {
  const { patches } = await open(page, "suggest");
  await expect(box(page)).toBeDisabled();
  await expect(
    page.locator('[data-testid="mission-auto-choose-why"]:visible'),
  ).toContainText("Needs autonomy set to yolo");
  expect(patches).toEqual([]);
});

test("an answer mission control gave on its own is in the thread", async ({
  page,
}) => {
  await open(page, "yolo", {
    auto_choose: true,
    events: [
      {
        seq: 7,
        mission_id: MISSION.id,
        at: T + 60,
        kind: "action",
        session_key: "claude:aaaaaaaa-0000-4000-8000-000000000001",
        action_id: "act_1",
        text: "mission control answered the menu itself: 2. Offline, in one transaction",
        meta: {
          auto_choose: true,
          option: 2,
          label: "Offline, in one transaction",
          confidence: 0.95,
        },
        settlement: null,
      },
    ],
  });
  await expect(box(page)).toBeChecked();
  await expect(
    page
      .getByText(
        "mission control answered the menu itself: 2. Offline, in one transaction",
      )
      .first(),
  ).toBeVisible();
});
