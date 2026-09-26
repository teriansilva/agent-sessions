/** The live strip above the mission thread (#1064 Phase 2), in a real browser.
 *
 *  What it pins: a running mission shows one line per held session, in words, from the `/now`
 *  route; only a producing session is green, and "at a prompt" is NOT amber (the bell and the
 *  decision cards own asking); a recap older than the latest output says so; a finished mission
 *  shows no strip; and the strip re-polls, so a status change reaches the page without a reload.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";
import { mockRoster } from "./roster";

const T = 1_700_000_000;

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: { configured: true },
};

async function stub(page: Page) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
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
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 1,
        generated_at: T - 60,
        window_days: 3,
        scan_depth: "fast",
        input_fingerprint: "fp",
        synthesis_skipped: false,
        cards: [],
      },
    }),
  );
  await page.route("**/api/pulse/orchestrator", (r) =>
    r.fulfill({ status: 500, json: { detail: "off" } }),
  );
}

const KEY_A = "claude:3f2a0000-0000-4000-8000-000000000000";
const KEY_B = "opencode:ses_ab12cd";

function row(key: string, over: Record<string, unknown>) {
  return {
    session_key: key,
    status: "producing",
    seconds_since_output: 4,
    prompt_class: null,
    recap_age_s: null,
    recap_older_than_output: false,
    ...over,
  };
}

async function openConsole(
  page: Page,
  state: string,
  now: () => unknown,
): Promise<{ calls: () => number }> {
  await stub(page);
  await mockMissions(page, {
    missions: missionList([
      missionRow({ state, session_keys: [KEY_A, KEY_B] }),
    ]),
    mission: { ...MISSION, state, events: [], events_next_seq: null },
  });
  let n = 0;
  // Registered after `mockMissions`, so it answers ahead of the helper's default.
  await page.route("**/api/missions/*/now", (r) => {
    n += 1;
    return r.fulfill({ json: now() });
  });
  await mockRoster(page); // the manifest-generated roster (#853 P4)
  await page.goto("/mission");
  await openMissionRail(page);
  await page
    .getByRole("navigation", { name: /missions/i })
    .getByRole("button", { name: /Kimi transcript adapter/i })
    .first()
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  return { calls: () => n };
}

const strip = (page: Page) =>
  page.locator('[data-testid="mission-now"]:visible');

test("a running mission says what each session is doing, in words", async ({
  page,
}) => {
  await openConsole(page, "running", () => ({
    checked_at: T,
    sessions: [
      row(KEY_A, { status: "producing", seconds_since_output: 4 }),
      row(KEY_B, {
        status: "at_prompt",
        prompt_class: "choice",
        seconds_since_output: 30,
        recap_age_s: 180,
        recap_older_than_output: true,
      }),
    ],
  }));
  const s = strip(page);
  await expect(s).toBeVisible();
  const rows = s.getByTestId("mission-now-row");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("claude · 3f2a");
  await expect(rows.nth(0)).toContainText("producing output · last 4s ago");
  await expect(rows.nth(1)).toContainText("opencode · ab12");
  await expect(rows.nth(1)).toContainText("waiting at a choice · quiet 30s");
  await expect(rows.nth(1)).toContainText(
    "recap 3m ago · written before the latest output",
  );

  // Only producing is green; at-a-prompt is neutral, never the needs-you amber.
  const dotColour = (i: number) =>
    rows
      .nth(i)
      .locator("span")
      .first()
      .evaluate((el) => getComputedStyle(el).backgroundColor);
  const token = (name: string) =>
    page.evaluate((n) => {
      const probe = document.createElement("span");
      probe.style.backgroundColor = `var(${n})`;
      document.body.appendChild(probe);
      const c = getComputedStyle(probe).backgroundColor;
      probe.remove();
      return c;
    }, name);
  expect(await dotColour(0)).toBe(await token("--status-up"));
  expect(await dotColour(1)).not.toBe(await token("--status-degraded"));
  expect(await dotColour(1)).not.toBe(await token("--status-up"));

  // The strip sits inside the pane and does not overflow it (mobile included).
  const box = await s.boundingBox();
  const vw = page.viewportSize()!.width;
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(vw + 0.5);
});

test("the strip re-polls, so a change reaches the page without a reload", async ({
  page,
}) => {
  let status = "producing";
  const { calls } = await openConsole(page, "running", () => ({
    checked_at: T,
    sessions: [
      row(KEY_A, {
        status,
        seconds_since_output: status === "quiet" ? 125 : 2,
      }),
    ],
  }));
  const r = strip(page).getByTestId("mission-now-row");
  await expect(r).toContainText("producing output");
  status = "quiet";
  await expect(r).toContainText("quiet for 2m", { timeout: 15_000 });
  expect(calls()).toBeGreaterThanOrEqual(2);
  await expect(r).toHaveAttribute("data-status", "quiet");
});

test("a finished mission shows no strip and asks for none", async ({
  page,
}) => {
  const { calls } = await openConsole(page, "done", () => ({
    checked_at: T,
    sessions: [],
  }));
  await expect(
    page.getByText("Nothing has happened yet.").first(),
  ).toBeVisible();
  await expect(page.locator('[data-testid="mission-now"]')).toHaveCount(0);
  expect(calls()).toBe(0);
});
