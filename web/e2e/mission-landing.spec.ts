/** The Missions section's front door (#948 P3).
 *
 * Entering the section opens the new-mission page with nothing selected — even while a mission
 * needs the operator. What needs them is previewed on the landing from the rail's own rows, and
 * that preview names its scope instead of pretending to be global. No workspace tabs; below 1400px
 * the details are one disclosure that never unmounts the thread. No untracked-session surface. The
 * composer takes templates, insert-only, and refuses to start a brief the server would truncate.
 */
import { expect, test, type Page } from "@playwright/test";
import { MISSION_PATH, missionLink } from "../src/lib/missionLink";
import {
  MISSION,
  flipMissionScope,
  missionList,
  missionRow,
  mockMissions,
  openMissionRail,
} from "./mission-console";

const NEEDY = "msn_" + "1".repeat(32);
const OTHER = "msn_" + "2".repeat(32);
const CREATED = "msn_" + "3".repeat(32);

const TEMPLATE = {
  id: "tpl_1",
  name: "PR checklist",
  description: "Ship a fix with a PR",
  tags: [],
  body: "Fix {{area}} and open a PR",
  fields: [{ name: "area", label: "Area", default: "auth", required: true }],
  images: [{ name: "shot.png", path: "/tmp/uploads/20260914-shot.png" }],
  created_at: 1,
  updated_at: 1,
  used_count: 0,
  last_used_at: null,
};

async function setup(page: Page, opts: { cards?: unknown[]; config?: Record<string, unknown> } = {}) {
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
        ...opts.config,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p1", name: "Alpha", folders: ["/repo/alpha"] },
          { id: "p2", name: "Beta", folders: ["/repo/beta"] },
        ],
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: opts.cards ?? [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/templates**", (r) => r.fulfill({ json: { templates: [TEMPLATE] } }));
  const details: string[] = [];
  page.on("request", (req) => {
    const m = /\/api\/missions\/(msn_[0-9a-f]{32})(\?|$)/.exec(req.url());
    if (m && req.method() === "GET") details.push(m[1]);
  });
  await mockMissions(page, {
    missions: (q) => {
      if (q.get("archived") === "1") return { ...missionList([]), facets: { projects: [], states: [] } };
      const rows = [
        missionRow({ id: NEEDY, title: "Needy mission", project_id: "p1", needs_you: true }),
        missionRow({ id: OTHER, title: "Quiet mission", project_id: "p2" }),
      ].filter((m) => !q.get("project") || m.project_id === q.get("project"));
      return { ...missionList(rows), facets: { projects: ["p1", "p2"], states: ["running"] } };
    },
  });
  await page.route(/\/api\/missions\/msn_[0-9a-f]{32}(\?.*)?$/, (r) => {
    const id = /msn_[0-9a-f]{32}/.exec(r.request().url())![0];
    const title = id === NEEDY ? "Needy mission" : id === CREATED ? "Created mission" : "Quiet mission";
    return r.fulfill({ json: { ...MISSION, id, title, events: [], events_next_seq: null } });
  });
  return { details };
}

test("entering Missions opens the new-mission page, even while a mission needs you", async ({ page }) => {
  const { details } = await setup(page);
  await page.goto(MISSION_PATH);
  await expect(page.getByTestId("mission-landing")).toBeVisible();
  await expect(page.getByRole("heading", { name: "What should this mission achieve?" })).toBeVisible();
  // Nothing was selected for the operator: no mission header, no mission read.
  await expect(page.getByTestId("console-title")).toHaveCount(0);
  await expect(page.getByTestId("landing-needs-row")).toContainText("Needy mission");
  expect(details).toEqual([]);
  // The brief does not grab focus on arrival (a phone would pop its keyboard).
  await expect(page.getByTestId("new-mission-instruction")).not.toBeFocused();

  // The preview row is the way in.
  await page.getByTestId("landing-needs-row").click();
  await expect(page.getByTestId("console-title")).toHaveText("Needy mission");
});

test("a retained filter that hides the needy mission says so, and Clear filters brings it back", async ({
  page,
}, testInfo) => {
  await setup(page);
  await page.goto(MISSION_PATH);
  await openMissionRail(page);
  await page.getByLabel("Filter missions by project").selectOption("p2");
  if (testInfo.project.name === "mobile") await page.keyboard.press("Escape");

  await expect(page.getByTestId("landing-needs-scope")).toContainText("in current filters");
  await expect(page.getByTestId("landing-needs-empty")).toHaveText("Nothing needs you in these filters.");
  await expect(page.getByText("Nothing needs you", { exact: true })).toHaveCount(0);

  await page.getByTestId("landing-clear-filters").click();
  await expect(page.getByTestId("landing-needs-row")).toContainText("Needy mission");
  await expect(page.getByTestId("landing-needs-scope")).toHaveCount(0);
});

test("the Archived scope is a filter too: scope note, empty preview, Clear filters back to Active", async ({
  page,
}, testInfo) => {
  await setup(page);
  await page.goto(MISSION_PATH);
  await openMissionRail(page);
  await flipMissionScope(page);
  if (testInfo.project.name === "mobile") await page.keyboard.press("Escape");
  await expect(page.getByTestId("landing-needs-empty")).toBeVisible();
  await expect(page.getByTestId("landing-needs-scope")).toContainText("in current filters");
  await page.getByTestId("landing-clear-filters").click();
  await expect(page.getByTestId("landing-needs-row")).toContainText("Needy mission");
  await openMissionRail(page);
  await expect(page.locator('[data-testid="rail-scope-active"]:visible').first()).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("a filtered read that is pending or failed claims nothing about what needs you", async ({
  page,
}, testInfo) => {
  // #959 review 4805, finding 3. Changing a filter clears the rail's rows before the new read
  // answers, and the landing used to render that empty array as "Needs you · 0 / Nothing needs you
  // in these filters" — a claim made from no answer at all, and still made when the read failed.
  await setup(page);
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  let searched = 0;
  // Registered AFTER `setup`, so it is matched first; every unsearched listing falls through.
  await page.route(/\/api\/missions\?/, async (r) => {
    if (!new URL(r.request().url()).searchParams.get("q")) return r.fallback();
    searched += 1;
    await held;
    return r.fulfill({ status: 503, json: { detail: "mission store unavailable" } });
  });
  await page.goto(MISSION_PATH);
  await expect(page.getByTestId("landing-needs-row")).toContainText("Needy mission");

  await openMissionRail(page);
  await page.getByLabel("Search missions").fill("needy");
  if (testInfo.project.name === "mobile") await page.keyboard.press("Escape");
  await expect.poll(() => searched).toBe(1);

  const needs = page.getByTestId("landing-needs-you");
  // Pending: no count, no empty claim, a loading line — and the way back is still offered.
  await expect(page.getByTestId("landing-needs-empty")).toHaveCount(0);
  await expect(needs.getByTestId("landing-needs-loading")).toBeVisible();
  await expect(needs).not.toContainText(/Needs you · \d/);
  await expect(page.getByTestId("landing-clear-filters")).toBeVisible();

  // Failed: says the missions could not be read, still claims nothing, still offers Clear filters.
  release();
  await expect(needs.getByTestId("landing-needs-error")).toBeVisible();
  await expect(needs.getByTestId("landing-needs-loading")).toHaveCount(0);
  await expect(page.getByTestId("landing-needs-empty")).toHaveCount(0);
  await expect(page.getByText("Nothing needs you in these filters.")).toHaveCount(0);
  await expect(needs).not.toContainText(/Needs you · \d/);
  await expect(page.getByTestId("landing-clear-filters")).toBeVisible();
});

test("a filtered first page with nothing needing you says it only read that page", async ({ page }, testInfo) => {
  // #959 review 4814. The listing is newest-first, not attention-first, so a project with 101 missions
  // can hold the one that needs you on page two. A SUCCESSFUL but PARTIAL filtered read used to say
  // "in current filters" / "Nothing needs you in these filters." — an all-clear for rows it never read.
  await setup(page);
  // Registered AFTER `setup`, so it is matched first; every other listing falls through.
  await page.route(/\/api\/missions\?/, (r) => {
    if (new URL(r.request().url()).searchParams.get("project") !== "p2") return r.fallback();
    return r.fulfill({
      json: {
        ...missionList([missionRow({ id: OTHER, title: "Quiet mission", project_id: "p2" })]),
        total: 101,
        facets: { projects: ["p1", "p2"], states: ["running"] },
      },
    });
  });
  await page.goto(MISSION_PATH);
  await openMissionRail(page);
  await page.getByLabel("Filter missions by project").selectOption("p2");
  if (testInfo.project.name === "mobile") await page.keyboard.press("Escape");

  await expect(page.getByTestId("landing-needs-scope")).toContainText("in the loaded missions within current filters");
  await expect(page.getByTestId("landing-needs-empty")).toHaveText(
    "Nothing needs you in the loaded missions within these filters.",
  );
  await expect(page.getByText("Nothing needs you in these filters.", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("landing-clear-filters")).toBeVisible();
});

test("no workspace tabs; the details disclosure never unmounts the thread's draft", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "width-driven layout, measured on the desktop project");
  await setup(page);
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.goto(missionLink(OTHER));
  await expect(page.getByTestId("console-title")).toHaveText("Quiet mission");
  await expect(page.getByRole("tablist", { name: "Mission view" })).toHaveCount(0);

  const draft = page.locator('[data-testid="split"] textarea').first();
  await draft.fill("half a thought");
  const node = await draft.elementHandle();
  const toggle = page.getByTestId("details-toggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTestId("mission-details")).toBeVisible();
  // The SAME textarea node, still attached, still holding the draft — no remount, no move.
  expect(await node!.evaluate((el) => (el as HTMLTextAreaElement).isConnected && (el as HTMLTextAreaElement).value)).toBe(
    "half a thought",
  );
  await toggle.click();
  await expect(page.getByTestId("mission-details")).toBeHidden();
  expect(await node!.evaluate((el) => (el as HTMLTextAreaElement).value)).toBe("half a thought");

  // At 1400px and above the details sit beside the thread and the toggle is gone.
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(toggle).toBeHidden();
  await expect(page.getByTestId("mission-details")).toBeVisible();
  await expect(page.getByRole("tablist", { name: "Mission view" })).toHaveCount(0);
});

test("no untracked-session surface anywhere in the section", async ({ page }, testInfo) => {
  await setup(page, {
    cards: [
      {
        id: "claude:cccccccc-1111-2222-3333-444444444444",
        engine: "claude",
        title: "An unadopted session",
        state: "needs_you",
        live: true,
        mission_id: null,
        project: { kind: "folder", id: "/repo/x", name: "/repo/x" },
      },
    ],
  });
  await page.goto(MISSION_PATH);
  await expect(page.getByTestId("mission-landing")).toBeVisible();
  if (testInfo.project.name === "mobile") await openMissionRail(page);
  await expect(page.getByTestId("rail-untracked-view")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/without a mission/i);
  await expect(page.locator("body")).not.toContainText("An unadopted session");
});

test("the landing is the brief form, and ASK has left for its own section (#1058)", async ({
  page,
}) => {
  // It used to be "ASK stays reachable from the landing" — one press of a segmented control. Ask is
  // `/ask` now, which is the whole point: a question about SESSIONS no longer lives behind the
  // MISSIONS section. What has to stay true is that neither surface lost anything — the landing is
  // the brief with no mode to pick, and Ask is one labelled entry away in the same top bar.
  await setup(page);
  await page.goto(MISSION_PATH);
  await expect(page.getByTestId("new-mission-form")).toBeVisible();
  await expect(page.getByTestId("composer-mode-new")).toHaveCount(0);
  await expect(page.getByTestId("composer-mode-ask")).toHaveCount(0);

  const ask = page
    .locator(".hud-topbar")
    .getByRole("navigation", { name: "Main sections" })
    .getByRole("link", { name: "Ask", exact: true });
  await expect(ask).toHaveAttribute("href", "/ask");
  await ask.click();
  await expect(page.getByTestId("composer-input")).toBeVisible();
});

test("a template inserts into the brief without creating anything; Start sends exactly that text", async ({
  page,
}) => {
  await setup(page);
  const creates: unknown[] = [];
  await page.route(/\/api\/missions$/, async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates.push(r.request().postDataJSON());
    return r.fulfill({ status: 201, json: { ...MISSION, id: CREATED, title: "Created mission" } });
  });
  await page.goto(MISSION_PATH);
  await page.getByTestId("new-mission-template").click();
  const picker = page.getByRole("dialog");
  await picker.getByRole("button", { name: /PR checklist/ }).first().click();
  // A mission has no session to send into, so the picker offers Insert only.
  await expect(picker.getByRole("button", { name: /^Send PR checklist/ })).toHaveCount(0);
  await picker.getByRole("button", { name: "Insert PR checklist into mission brief" }).click();

  const brief = page.getByTestId("new-mission-instruction");
  await expect(brief).toHaveValue("Fix auth and open a PR /tmp/uploads/20260914-shot.png");
  expect(creates).toEqual([]);

  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();
  await expect.poll(() => creates.length).toBe(1);
  expect(creates[0]).toMatchObject({
    instruction: "Fix auth and open a PR /tmp/uploads/20260914-shot.png",
    project_id: "p1",
  });
});

test("the workspace type scale is the §4 one, not 0.66–0.84rem", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "computed once, on the desktop project");
  await setup(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  const type = (loc: import("@playwright/test").Locator) =>
    loc.evaluate((el) => {
      const s = getComputedStyle(el);
      return [s.fontSize, s.fontWeight];
    });

  await page.goto(MISSION_PATH);
  // The landing brief is the centrepiece: 16px. The footer's mono chrome is 11px — measured on the
  // shortcut hint since #1058 retired the mode control that used to carry that value.
  expect((await type(page.getByTestId("new-mission-instruction")))[0]).toBe("16px");
  expect((await type(page.getByTestId("new-mission-hint")))[0]).toBe("11px");

  // A selected mission: title 16px/600, the thread composer at the 14px body size.
  await page.goto(missionLink(OTHER));
  await expect(page.getByTestId("console-title")).toHaveText("Quiet mission");
  expect(await type(page.getByTestId("console-title"))).toEqual(["16px", "600"]);
  expect((await type(page.locator('[data-testid="split"] textarea').first()))[0]).toBe("14px");
});

test("a brief over the server's 8000-character cap cannot be started", async ({ page }) => {
  await setup(page);
  await page.goto(MISSION_PATH);
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-instruction").fill("x".repeat(8001));
  await expect(page.getByTestId("new-mission-count")).toContainText("8001 / 8000");
  await expect(page.getByTestId("new-mission-start")).toBeDisabled();
  await page.getByTestId("new-mission-instruction").fill("x".repeat(7999));
  await expect(page.getByTestId("new-mission-start")).toBeEnabled();
});

test("Ctrl/⌘+Enter honours the cap too: an over-cap brief is not created", async ({ page }) => {
  // `requestSubmit()` submits a form whose submit button is disabled, so the button alone never
  // guarded the keyboard path.
  await setup(page);
  const creates: { instruction: string }[] = [];
  await page.route(/\/api\/missions$/, async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates.push(r.request().postDataJSON());
    return r.fulfill({ status: 201, json: { ...MISSION, id: CREATED, title: "Created mission" } });
  });
  await page.goto(MISSION_PATH);
  await page.getByTestId("new-mission-project").selectOption("p1");
  const brief = page.getByTestId("new-mission-instruction");
  await brief.fill("x".repeat(8001));
  await brief.press("Control+Enter");
  // Then a legal brief by the same key. If the first press had gone through, the FIRST create
  // would carry 8001 characters.
  await brief.fill("x".repeat(7999));
  await brief.press("Control+Enter");
  await expect.poll(() => creates.length).toBe(1);
  expect(creates[0].instruction).toHaveLength(7999);
});

test("a template appends to an existing brief on a new line, and a cancelled picker changes nothing", async ({
  page,
}) => {
  await setup(page);
  await page.goto(MISSION_PATH);
  const brief = page.getByTestId("new-mission-instruction");
  await brief.fill("Keep the retry budget   ");

  // Cancel: pick a template, then dismiss the picker without inserting.
  await page.getByTestId("new-mission-template").click();
  const picker = page.getByRole("dialog");
  await picker.getByRole("button", { name: /PR checklist/ }).first().click();
  await page.keyboard.press("Escape");
  await expect(picker).toBeHidden();
  await expect(brief).toHaveValue("Keep the retry budget   ");

  // Insert into a non-empty brief: trailing space trimmed, the template on its own line.
  await page.getByTestId("new-mission-template").click();
  await picker.getByRole("button", { name: /PR checklist/ }).first().click();
  await picker.getByRole("button", { name: "Insert PR checklist into mission brief" }).click();
  await expect(brief).toHaveValue(
    "Keep the retry budget\nFix auth and open a PR /tmp/uploads/20260914-shot.png",
  );
});

test("the checklist is chosen per mission: default pre-selected, the pick is what is sent (#1061)", async ({
  page,
}) => {
  await setup(page, {
    config: {
      mission_playbooks: {
        default_id: "pr",
        revision: 1,
        playbooks: [
          { id: "pr", label: "Ship a PR", objectives: [] },
          { id: "audit", label: "Dependency audit", objectives: [] },
        ],
      },
    },
  });
  const creates: Record<string, unknown>[] = [];
  await page.route("**/api/missions", (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    creates.push(r.request().postDataJSON());
    return r.fulfill({ status: 201, json: { ...MISSION, id: CREATED, title: "Created mission" } });
  });
  await page.goto(MISSION_PATH);
  const pick = page.getByTestId("new-mission-playbook");
  await expect(pick).toHaveValue("pr");
  await expect(pick.locator("option")).toHaveText([
    "Checklist: Ship a PR",
    "Checklist: Dependency audit",
    "No checklist",
  ]);

  // It sits in the composer footer and stays inside it, phone included.
  const foot = await page.getByTestId("composer-foot").boundingBox();
  const box = await pick.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(foot!.x - 0.5);
  expect(box!.x + box!.width).toBeLessThanOrEqual(foot!.x + foot!.width + 0.5);
  expect(box!.height).toBeGreaterThanOrEqual(44);
  const vw = page.viewportSize()!.width;
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(vw);

  await expect(page.getByTestId("new-mission-playbook-note")).toContainText("Ship a PR —");
  await pick.selectOption(":none");
  // Declining states its cost under the box, in the text-safe amber.
  const warn = page.getByTestId("new-mission-playbook-note");
  await expect(warn).toContainText("never confirm itself finished");
  const colours = await warn.evaluate((el) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--warn-text)";
    document.body.appendChild(probe);
    const want = getComputedStyle(probe).color;
    probe.remove();
    return [getComputedStyle(el).color, want];
  });
  expect(colours[0]).toBe(colours[1]);
  await page.getByTestId("new-mission-instruction").fill("tidy the readme");
  await page.getByTestId("new-mission-project").selectOption("p1");
  await page.getByTestId("new-mission-start").click();
  await expect.poll(() => creates.length).toBe(1);
  expect(creates[0]).toMatchObject({ instruction: "tidy the readme", project_id: "p1", playbook_id: ":none" });
});

test("the AI 'No checklist' label gives way in the footer, never onto Template", async ({
  page,
}, testInfo) => {
  // #1133. With an AI endpoint the closed label is "No checklist — AI writes the objectives"
  // (#1088) — wider than any playbook name, and wider than what the landing leaves beside
  // Template: the box caps at 720px, the lead is squeezed (min-width: 0) and the picker used to
  // refuse to shrink, so it overflowed its lead 22px ONTO the Template button. It gives way like
  // the project picker now. The single-row branch only exists above the 660px container
  // breakpoint (the wrapped branch gives the picker a row of its own), so this is a desktop-width
  // assertion.
  test.skip(testInfo.project.name !== "desktop", "the collision is the single-row footer branch, a >660px box");
  await setup(page, {
    config: {
      ai_review: { configured: true },
      mission_playbooks: {
        default_id: ":none",
        revision: 1,
        playbooks: [{ id: "pr", label: "Ship a PR", objectives: [] }],
      },
    },
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(MISSION_PATH);
  const pick = page.getByTestId("new-mission-playbook");
  await expect(pick).toHaveValue(":none");

  const rects = await page.evaluate(() => {
    const r = (t: string) =>
      document.querySelector(`[data-testid="${t}"]`)!.getBoundingClientRect();
    return {
      pick: r("new-mission-playbook"),
      template: r("new-mission-template"),
      foot: r("composer-foot"),
      start: r("new-mission-start"),
      scrollW: document.documentElement.scrollWidth,
    };
  });
  // The branch this pins: ONE footer row — picker and Template share it. (On the wrapped branch
  // the horizontal ranges legitimately overlap; it is a different row.)
  expect(Math.abs(rects.pick.y - rects.template.y)).toBeLessThan(2);
  // No collision: the picker's right edge stops short of Template's left edge …
  expect(rects.pick.right).toBeLessThanOrEqual(rects.template.x + 0.5);
  // … and nothing leaves the footer or the page.
  expect(rects.template.right).toBeLessThanOrEqual(rects.foot.right + 0.5);
  expect(rects.start.right).toBeLessThanOrEqual(rects.foot.right + 0.5);
  expect(rects.scrollW).toBeLessThanOrEqual(1280);
});
