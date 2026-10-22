/** The mission rail and the route shell in NIGHTJAR (#1573, Phase 8), in a real browser.
 *
 *  - Each row's LED is the shared `hud-led` with the tone of its state (needs you outranks the
 *    state; starting is neutral, never green; done is a hollow ring), and the list holds them
 *    STEADY in Full — a column of breathing dots is noise and idle main-thread time.
 *  - A mission that joins the rail after it loaded ARRIVES (the session rows' `.rowArrived`); the
 *    first load does not.
 *  - The route's overview read failing is a state with a fix: Retry reads it again.
 */
import { expect, test, type Page } from "@playwright/test";

import { MISSION_PATH } from "../src/lib/missionLink";
import { missionList, missionRow, mockMissions, openMissionRail } from "./mission-console";
import { storeMotionChoice } from "./nightjarShell";

const id = (n: number) => "msn_" + String(n).repeat(32);
const ROWS = [
  missionRow({ id: id(1), title: "Needs the operator", state: "running", needs_you: true }),
  missionRow({ id: id(2), title: "Working away", state: "running" }),
  missionRow({ id: id(3), title: "Just starting", state: "dispatching" }),
  missionRow({ id: id(4), title: "It broke", state: "failed" }),
  missionRow({ id: id(5), title: "A draft", state: "draft" }),
  missionRow({ id: id(6), title: "All done", state: "done" }),
];

async function setup(page: Page, opts: { pulseFails?: number } = {}) {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await storeMotionChoice(page, "full");
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "Alpha", folders: ["/repo/alpha"] }] } }),
  );
  let pulseFails = opts.pulseFails ?? 0;
  let pulseReads = 0;
  await page.route(/\/api\/pulse$/, (r) => {
    pulseReads += 1;
    if (pulseFails > 0) {
      pulseFails -= 1;
      return r.fulfill({ status: 503, json: { detail: "restarting" } });
    }
    return r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } });
  });
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  const rows = [...ROWS];
  await mockMissions(page, {
    missions: () => ({ ...missionList(rows), facets: { projects: [], states: ["running"] } }),
  });
  return {
    rows,
    pulseReads: () => pulseReads,
  };
}

/** The LED in a rail row, by the row's title. */
function led(page: Page, title: string) {
  return page.getByTestId("rail-mission").filter({ hasText: title }).locator(".hud-led");
}

test("each row's LED carries its state's tone, steady in the list (#1573)", async ({ page }) => {
  await setup(page);
  await page.goto(MISSION_PATH);
  await openMissionRail(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(ROWS.length);

  const tones = {
    "Needs the operator": "att",
    "Working away": "up",
    "Just starting": "idle",
    "It broke": "down",
    "A draft": "draft",
    "All done": "idle",
  };
  for (const [title, tone] of Object.entries(tones)) {
    await expect(led(page, title), title).toHaveAttribute("data-tone", tone);
  }
  // The words carry the state; needs you says so to a screen reader too.
  await expect(led(page, "Needs the operator")).toHaveAttribute("aria-label", "Needs you");

  const look = (title: string) =>
    led(page, title).evaluate((el) => {
      const s = getComputedStyle(el);
      return { bg: s.backgroundColor, shadow: s.boxShadow, anim: s.animationName };
    });
  // STARTING is not running: not the green the running row is lit in.
  expect((await look("Just starting")).bg).not.toBe((await look("Working away")).bg);
  // Done is a hollow ring, not a light.
  const done = await look("All done");
  expect(done.bg).toBe("rgba(0, 0, 0, 0)");
  expect(done.shadow).toContain("inset");
  // Steady in Full: no breathing or blinking down the list (a state change still flashes).
  for (const title of Object.keys(tones)) expect((await look(title)).anim, title).toBe("none");
});

test("a mission that joins the rail ARRIVES; the first load does not (#1573)", async ({ page }) => {
  const { rows } = await setup(page);
  await page.route(/\/api\/missions(\?.*)?$/, async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    const made = missionRow({ id: id(7), title: "Freshly started", project_id: "p1", state: "dispatching" });
    rows.unshift(made);
    return r.fulfill({ json: made });
  });
  await page.goto(MISSION_PATH);
  await openMissionRail(page);
  await expect(page.getByTestId("rail-mission")).toHaveCount(ROWS.length);
  // The first load is a list arriving, not a row: nothing slides in.
  for (const row of await page.getByTestId("rail-mission").all())
    await expect(row).not.toHaveClass(/rowArrived/);
  await page.keyboard.press("Escape");

  await page.getByTestId("new-mission-instruction").fill("Start something new");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();

  await openMissionRail(page);
  const fresh = page.getByTestId("rail-mission").filter({ hasText: "Freshly started" });
  await expect(fresh).toHaveCount(1);
  await expect(fresh).toHaveClass(/rowArrived/);
  // …and it comes off once the arrival has played.
  await expect(fresh).not.toHaveClass(/rowArrived/, { timeout: 3000 });
});

test("the route's failed overview read is a state with a fix: Retry reads it again (#1573)", async ({
  page,
}) => {
  const api = await setup(page, { pulseFails: 1 });
  await page.goto(MISSION_PATH);
  const err = page.getByTestId("pulse-error");
  await expect(err).toBeVisible();
  await expect(err).toContainText("Couldn’t load the overview.");
  const before = api.pulseReads();
  await page.getByTestId("pulse-retry").click();
  await expect(err).toHaveCount(0);
  expect(api.pulseReads()).toBeGreaterThan(before);
});
