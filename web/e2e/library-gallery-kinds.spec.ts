import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { mockRoster } from "./roster";
import {
  MISSION_PLANS_PATH,
  SKILLS_PATH,
  SPECIALISTS_PATH,
  missionPlanPath,
  specialistPath,
} from "../src/lib/routes";

/** One Library gallery (#2144 P2): Skills, Specialists and Mission plans wear the same card, grid
 *  and detail as Playbooks. A tap anywhere on a card opens it; its own controls (Export, a held
 *  slot's Release) act without opening it; Import from file closes every grid; the page headers
 *  carry no Import…/Export…. Desktop and phone. */

const SKILLS = {
  skills: [
    {
      id: "code-review",
      revision: "4be019c" + "0".repeat(57),
      files: { "SKILL.md": "# Code review\n", "checklist.md": "- read the diff\n" },
      editable: true,
      gallery_origin: {
        state: "verified", kind: "skill", id: "code-review", version: "1.3", title: "Code review",
        publisher: "BattleLab", entry_digest: "e".repeat(64), base_revision: "4be019c" + "0".repeat(57),
      },
    },
    { id: "changelog-writer", revision: "91d2a07" + "0".repeat(57), files: { "SKILL.md": "# Changelog\n" }, editable: true },
  ],
};
const version = (o: Record<string, unknown>) => ({
  version: 4, title: "Reviewer", summary: "Reads every diff before merge.", skills: [{ id: "code-review", revision: "r" }],
  engine: "claude", model: "opus", effort: null, capacity: 2, created_at: 1, ...o,
});
const SPECIALISTS = {
  specialists: [
    {
      id: "reviewer", revision: 3, archived: false, grade: "frontier", live: 1,
      skills_missing: [], skills_outdated: [], current: version({}),
      held: [{ reservation_id: "r" + "a".repeat(24), session_key: "claude:held", reason: "the session's state is unknown", since: 1, attempt: 3, rev: 7 }],
    },
    {
      id: "retired", revision: 2, archived: true, grade: null, live: 0,
      skills_missing: [], skills_outdated: [], current: version({ title: "Retired", version: 1 }),
    },
  ],
  templates: [],
  max_instances: 8,
  live_total: 1,
};
const counts = { objectives: 2, slots: 1, steps: 3 };
const PLAN = {
  id: "plan-1a2b3c4d", version: 2, revision: 1, archived: false, title: "Monthly invoice run",
  summary: "Collect, file, reconcile.", origin_kind: "mission", created_at: 1, updated_at: 1, counts,
};
const PLAN_DETAIL = {
  ...PLAN,
  document: {
    format: 1, title: PLAN.title, summary: PLAN.summary,
    slots: [{ id: "p1", label: "Mail" }],
    objectives: [{ id: "o1", slot: "p1", procedure: { source: "mission_local", flow_id: "dev", title: "Triage" }, inputs: [] }],
  },
  steps: [{ id: "o1.find", objective: "o1", title: "Find the invoices", actor: "agent", after: [], role: null }],
};

interface World {
  writes: { path: string; body: unknown }[];
}

async function setup(page: Page): Promise<World> {
  await commonMocks(page);
  await mockRoster(page);
  const world: World = { writes: [] };
  const record = (path: string, body: unknown) => world.writes.push({ path, body });
  const copies: string[] = [];
  await page.route("**/api/library/skills**", async (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (path === "/api/library/skills") return r.fulfill({ json: SKILLS });
    if (req.method() === "POST" && path.endsWith("/copies")) {
      const body = req.postDataJSON() as { new_id: string; operation: string };
      record(path, body);
      copies.push(body.new_id);
      // A save that takes a moment, like a real disk write: the page is still "busy" when it lands.
      await new Promise((resolve) => setTimeout(resolve, 400));
      return r.fulfill({
        json: {
          operation: body.operation,
          state: "complete",
          result: { kind: "skill", id: body.new_id, revision: "c".repeat(64), durable: true, gallery_origin: null },
        },
      });
    }
    const id = decodeURIComponent(path.split("/").pop()!);
    const found = SKILLS.skills.find((s) => s.id === id);
    if (found) return r.fulfill({ json: found });
    if (copies.includes(id)) return r.fulfill({ json: { ...SKILLS.skills[1], id, gallery_origin: null } });
    return r.fulfill({ status: 404, json: { detail: "not found" } });
  });
  await page.route("**/api/specialists**", (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET") {
      record(path, req.postDataJSON());
      return r.fulfill({ json: { released: true } });
    }
    if (path === "/api/specialists") return r.fulfill({ json: SPECIALISTS });
    const found = SPECIALISTS.specialists.find((x) => x.id === decodeURIComponent(path.split("/").pop()!));
    return found
      ? r.fulfill({ json: { ...found, versions: [found.current] } })
      : r.fulfill({ status: 404, json: { detail: "not found" } });
  });
  await page.route("**/api/mission-plans**", (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET") record(path, req.postDataJSON());
    return r.fulfill({ json: path === "/api/mission-plans" ? { plans: [PLAN] } : PLAN_DETAIL });
  });
  await page.route("**/api/share/**", (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET") record(path, req.postDataJSON());
    if (path.endsWith("/catalog"))
      return r.fulfill({
        json: {
          imports: {},
          items: [
            { kind: "skill", id: "code-review", title: "code-review", depends_on: [], exportable: true, reason: null, source: "local" },
            { kind: "skill", id: "changelog-writer", title: "changelog-writer", depends_on: [], exportable: true, reason: null, source: "local" },
            { kind: "specialist", id: "reviewer", title: "Reviewer", depends_on: ["skill:code-review"], exportable: true, reason: null, source: "local" },
            { kind: "mission-plan", id: PLAN.id, title: PLAN.title, depends_on: [], exportable: true, reason: null, source: "local" },
          ],
        },
      });
    if (path.endsWith("/imports")) return r.fulfill({ json: { imports: {} } });
    return r.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return world;
}

/** A tap (phone) or a click (desktop) on the card's summary/meta, away from its title link. */
async function tapBody(page: Page, testId: string, mobile: boolean) {
  const box = (await page.getByTestId(testId).locator("p").first().boundingBox())!;
  const at = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  if (mobile) await page.touchscreen.tap(at.x, at.y);
  else await page.mouse.click(at.x, at.y);
}

const KINDS = [
  { name: "Skills", path: SKILLS_PATH, card: "skill-card-code-review", opens: `${SKILLS_PATH}/code-review`, export: "Export Code review", chosen: "Skill code-review" },
  { name: "Specialists", path: SPECIALISTS_PATH, card: "specialist-card-reviewer", opens: specialistPath("reviewer"), export: "Export Reviewer", chosen: "Specialist Reviewer" },
  { name: "Mission plans", path: MISSION_PLANS_PATH, card: `mission-plan-card-${PLAN.id}`, opens: missionPlanPath(PLAN.id), export: `Export ${PLAN.title}`, chosen: `Mission plan ${PLAN.title}` },
];

for (const kind of KINDS) {
  test(`${kind.name}: a tap anywhere opens a card; its Export doesn't; Import from file closes the grid`, async ({
    page,
  }, info) => {
    await setup(page);
    await page.goto(kind.path);
    await expect(page.getByTestId(kind.card)).toBeVisible();
    // No header Import…/Export… any more, and Import from file is in the grid.
    await expect(page.getByRole("button", { name: "Import…" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Export…" })).toHaveCount(0);
    await expect(page.getByTestId("library-import-tile")).toBeVisible();
    // One link per card; nothing interactive nested in a link.
    expect(
      await page.locator("main[data-testid]").evaluate((m) => m.querySelectorAll("a a, a button, button a, button button").length),
    ).toBe(0);
    // The card's own Export: the dialog with that item chosen, and no navigation.
    await page.getByRole("button", { name: kind.export }).click();
    const dialog = page.getByTestId("share-export");
    await expect(dialog.getByLabel(kind.chosen)).toBeChecked();
    await expect(page).toHaveURL(new RegExp(`${kind.path}$`));
    await dialog.getByRole("button", { name: "Close" }).click();
    // The tile opens the import dialog.
    await page.getByTestId("library-import-tile").click();
    await expect(page.getByTestId("share-import")).toBeVisible();
    await page.getByTestId("share-import").getByRole("button", { name: "Close" }).click();
    // A tap on the card body opens it.
    await tapBody(page, kind.card, info.project.name === "mobile");
    await expect(page).toHaveURL(new RegExp(`${kind.opens}$`));
    // Every detail has the shared bar: Export and Import… (a plan's Export leads).
    const bar = page.getByRole("toolbar", { name: "Actions" });
    await expect(bar.getByRole("button", { name: "Export" })).toBeVisible();
    await expect(bar.getByRole("button", { name: "Import…" })).toBeVisible();
    await expect(bar.getByRole("button", { name: "More actions" })).toBeVisible();
  });
}

test("a specialist's held-slot Release acts on its own and never opens the card", async ({ page }) => {
  const world = await setup(page);
  await page.goto(SPECIALISTS_PATH);
  const held = page.getByTestId("specialist-card-reviewer").getByTestId("specialist-held");
  await expect(held).toContainText("the session's state is unknown");
  await held.getByRole("button", { name: "Release" }).click();
  await expect.poll(() => world.writes.filter((w) => w.path.includes("release")).length).toBe(1);
  await expect(page).toHaveURL(new RegExp(`${SPECIALISTS_PATH}$`));
});

test("Archived is a chip: specialists show only the archived ones; the New and Import tiles step aside", async ({
  page,
}) => {
  await setup(page);
  await page.goto(SPECIALISTS_PATH);
  await expect(page.getByTestId("specialist-card-retired")).toHaveCount(0);
  await page.getByRole("group", { name: "Show" }).getByRole("button", { name: /^Archived/ }).click();
  await expect(page.getByTestId("specialist-card-retired")).toContainText("Archived");
  await expect(page.getByTestId("specialist-card-reviewer")).toHaveCount(0);
  await expect(page.getByTestId("library-new-tile")).toHaveCount(0);
});

test("a mission plan's detail leads with Export (plans are inert); Archive is in its ⋯", async ({ page }) => {
  const world = await setup(page);
  await page.goto(missionPlanPath(PLAN.id));
  const bar = page.getByRole("toolbar", { name: "Actions" });
  const first = bar.getByRole("button").first();
  await expect(first).toHaveText("Export");
  await bar.getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Archive" }).click();
  await expect
    .poll(() => world.writes.filter((w) => w.path.endsWith("/archive")).map((w) => w.body))
    .toEqual([{ archived: true, expect_revision: 1 }]);
});

test("a skill's Duplicate saves one copy and opens it — no leave-the-draft question (#2256 review)", async ({
  page,
}) => {
  const world = await setup(page);
  await page.goto(`${SKILLS_PATH}/changelog-writer`);
  await expect(page.getByRole("heading", { level: 1, name: "changelog-writer" })).toBeVisible();
  await page.getByRole("toolbar", { name: "Actions" }).getByRole("button", { name: "More actions" }).click();
  await page.getByRole("menuitem", { name: "Duplicate" }).click();
  await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}/changelog-writer-copy$`));
  await expect(page.getByRole("heading", { name: "Leave this draft?" })).toHaveCount(0);
  expect(world.writes.filter((w) => w.path.endsWith("/copies")).map((w) => (w.body as { new_id: string }).new_id)).toEqual([
    "changelog-writer-copy",
  ]);
});

test("a skill with an unsaved draft still asks before leaving", async ({ page }) => {
  await setup(page);
  await page.goto(`${SKILLS_PATH}/changelog-writer`);
  await page.getByLabel("Your draft: SKILL.md").fill("My unsaved edit");
  await page.getByRole("link", { name: "All skills", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Leave this draft?" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${SKILLS_PATH}/changelog-writer$`));
});
