/** Mission plans in the Library (#1924, #1613 P7a), in a real browser on desktop and phone: the
 *  list (live plans, then Show archived ADDS the archived ones), the detail (slots, the one
 *  never-saved list, objectives in order with their role needs), archive with its revision fence,
 *  Save as mission plan from a mission's ⋯ menu (exactly what is sent, and nothing about the
 *  project), and the share dialogs' Mission plans group. The backend is mocked; every write the
 *  SPA sends is asserted. */
import { expect, test, type Page } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { MISSION, missionList, missionRow, mockMissions } from "./mission-console";
import { animationsSettled } from "./nightjarShell";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { MISSION_PLANS_PATH, missionPlanPath } from "../src/lib/routes";

const MID = "msn_" + "a".repeat(32);

const row = (over: Record<string, unknown>) => ({
  version: 1,
  revision: 1,
  archived: false,
  summary: "",
  origin_kind: "mission",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  counts: { objectives: 3, slots: 3, steps: 8 },
  ...over,
});
const PLANS = [
  row({ id: "plan-1a2b3c4d", title: "Monthly invoice run", summary: "Collect, file, reconcile." }),
  row({ id: "plan-5e6f7a8b", title: "Ship a change with review", origin_kind: "file", counts: { objectives: 1, slots: 1, steps: 4 } }),
  row({ id: "plan-9c0d1e2f", title: "Quarterly audit (old)", archived: true, revision: 2 }),
];
const flow = (title: string) => ({ source: "playbook", flow_id: "dev", title, playbook_id: "mail-ops" });
const DETAIL = {
  ...PLANS[0],
  document: {
    format: 1,
    title: "Monthly invoice run",
    summary: "Collect, file, reconcile.",
    slots: [
      { id: "p1", label: "Mail" },
      { id: "p2", label: "Files" },
    ],
    objectives: [
      { id: "o1", slot: "p1", procedure: flow("Triage the invoices"), inputs: ["repo"] },
      { id: "o2", slot: "p2", procedure: { ...flow("File them"), source: "mission_local" }, inputs: [], after: ["o1"] },
    ],
  },
  steps: [
    { id: "o1.find", objective: "o1", title: "Find the invoices", actor: "agent", after: [], role: { skills: ["mail-triage"], min_grade: "fast" } },
    { id: "o1.ok", objective: "o1", title: "Confirm the list", actor: "operator", after: ["o1.find"], role: null },
    { id: "o2.store", objective: "o2", title: "Store each invoice", actor: "agent", after: [], role: { skills: ["nextcloud-files"], min_grade: "strong" } },
  ],
};

async function mockLibrary(page: Page, writes: { url: string; body: unknown }[]) {
  await commonMocks(page);
  await mockRoster(page);
  await page.route("**/api/mission-plans**", async (r) => {
    const url = new URL(r.request().url());
    if (r.request().method() === "POST") {
      writes.push({ url: url.pathname, body: r.request().postDataJSON() });
      return r.fulfill({ json: { ...DETAIL, archived: true, revision: 2 } });
    }
    if (url.pathname === "/api/mission-plans") return r.fulfill({ json: { plans: PLANS } });
    return r.fulfill({ json: DETAIL });
  });
  const catalog = {
    imports: {},
    items: [
      ...PLANS.filter((p) => !p.archived).map((p) => ({
      kind: "mission-plan",
      id: p.id,
      title: p.title,
      depends_on: [],
      exportable: true,
      reason: null,
      source: "local",
      })),
      // A plan whose own text the scan refuses: listed by its random ID, never choosable.
      {
        kind: "mission-plan",
        id: "plan-0badf00d",
        title: "plan-0badf00d",
        depends_on: [],
        exportable: false,
        reason: "Its plan title holds one of your stored secrets; a mission plan can't be edited: save or import it again under another title.",
        source: "local",
      },
    ],
  };
  // ONE handler for the share surface: a catch-all beside specific routes shadows them.
  await page.route("**/api/share/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET")
      writes.push({ url: path, body: path.endsWith("/preview") ? "<file>" : request.postDataJSON() });
    if (path.endsWith("/catalog")) return route.fulfill({ json: catalog });
    if (path.endsWith("/imports")) return route.fulfill({ json: { imports: {} } });
    if (path.endsWith("/export")) return route.fulfill({ body: "bundle", headers: { "content-type": "application/x-tar" } });
    if (path.endsWith("/import/preview")) return route.fulfill({ json: PREVIEW });
    if (path.endsWith("/import"))
      return route.fulfill({
        json: { items: [{ key: "mission-plan:plan-3fa9c1d2", status: "created", dest: "plan-3fa9c1d2", reason: null, retry: false }] },
      });
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
}

const PREVIEW = {
  token: "b".repeat(32),
  expires_at: "2026-11-11T10:27:00Z",
  bundle: { digest: "c".repeat(64), size: 4096, format: 1, created_at: "2026-11-11T10:12:00Z", app_version: "0.27.0", source: null, items: 1, files: 1 },
  items: [
    {
      key: "mission-plan:plan-3fa9c1d2",
      kind: "mission-plan",
      id: "plan-3fa9c1d2",
      title: "Monthly invoice run",
      dest: "plan-3fa9c1d2",
      status: "new",
      reason: null,
      depends_on: [],
      uses: [],
      files: [{ path: "mission-plan.json", size: 10, text: "{}" }],
      "mission-plan": { objectives: 3, slots: 3, steps: 8 },
    },
  ],
};

test("the Library lists live plans, Show archived adds the archived one, and every control is a touch target", async ({ page }) => {
  const writes: { url: string; body: unknown }[] = [];
  await mockLibrary(page, writes);
  await page.goto(MISSION_PLANS_PATH);
  const cards = page.locator("[data-testid^='mission-plan-card-']");
  await expect(cards).toHaveCount(2);
  await expect(page.getByTestId("mission-plan-card-plan-5e6f7a8b")).toContainText("Imported from a file");
  await expect(page.getByTestId("mission-plan-card-plan-1a2b3c4d")).toContainText("3 objectives · 3 project slots · 8 steps");
  const toggle = page.getByTestId("mission-plans-show-archived");
  await expect(toggle).toHaveText("Show archived (1)");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(cards).toHaveCount(3); // added, never replacing the live ones
  await expect(page.getByTestId("mission-plan-card-plan-9c0d1e2f")).toContainText("Archived");
  for (const b of await page.locator("main [data-testid^='mission-plan-card-'] :is(a, button)").all()) {
    if (!(await b.isVisible())) continue;
    const box = await b.boundingBox();
    // A text link in a card's title is not a button; every action is a 44 px box.
    if ((await b.getAttribute("class"))?.includes("title")) continue;
    expect(box!.height, await b.innerText()).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  // Unarchive sends the revision the card was loaded at: the fence.
  await page.getByTestId("mission-plan-card-plan-9c0d1e2f").getByRole("button", { name: "Unarchive" }).click();
  await expect.poll(() => writes).toEqual([
    { url: "/api/mission-plans/plan-9c0d1e2f/archive", body: { archived: false, expect_revision: 2 } },
  ]);
});

test("the detail shows slots, the never-saved list and the objectives in order with their role needs", async ({ page }) => {
  const writes: { url: string; body: unknown }[] = [];
  await mockLibrary(page, writes);
  await page.goto(missionPlanPath("plan-1a2b3c4d"));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Monthly invoice run");
  await expect(page.getByTestId("mission-plan-inert")).toContainText("This plan runs nothing.");
  const never = page.getByRole("region", { name: "Never saved in a plan" });
  await expect(never.getByRole("listitem")).toHaveCount(4);
  await expect(never).toContainText("playbook defaults");
  const o1 = page.getByTestId("mission-plan-objective-o1");
  await expect(o1).toContainText("P1 · Mail");
  await expect(o1).toContainText("playbook · mail-ops");
  await expect(o1).toContainText("mail-triage");
  await expect(o1).toContainText("min fast");
  const o2 = page.getByTestId("mission-plan-objective-o2");
  await expect(o2).toContainText("mission-local procedure");
  await expect(o2).toContainText("after o1");
  // The chips are labels, never controls.
  expect(await o1.locator("button, a, [tabindex]").count()).toBe(0);
  // Export opens the share dialog with this plan already chosen.
  await page.getByRole("button", { name: "Export" }).click();
  const dialog = page.getByTestId("share-export");
  await expect(dialog.getByRole("region", { name: "Mission plans" })).toBeVisible();
  await expect(dialog.getByTestId("share-export-summary")).toContainText("1 chosen");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Archive" }).click();
  await expect.poll(() => writes).toEqual([
    { url: "/api/mission-plans/plan-1a2b3c4d/archive", body: { archived: true, expect_revision: 1 } },
  ]);
});

test("Save as mission plan sends the revision, the title and one label per slot — never a project", async ({ page }) => {
  const saves: unknown[] = [];
  await commonMocks(page);
  await mockRoster(page);
  await mockMissions(page, {
    missions: missionList([missionRow({ id: MID, title: "Invoices, October", state: "done" })]),
    mission: { ...MISSION, id: MID, title: "Invoices, October", state: "done", events: [], events_next_seq: null },
  });
  await page.route("**/api/projects**", (r) =>
    r.fulfill({
      json: {
        projects: [
          { id: "p-mail", name: "Acme mailbox", color: "", folders: ["/srv/mail"], default_folder: "/srv/mail", archived: false, created_at: 0, session_count: 0 },
          { id: "p-files", name: "Acme Nextcloud", color: "", folders: [], default_folder: "", archived: false, created_at: 0, session_count: 0 },
        ],
      },
    }),
  );
  await page.route(`**/api/missions/${MID}/plan/revisions/latest`, (r) =>
    r.fulfill({
      json: {
        mission_id: MID,
        plan: {
          revision: 3,
          document: {
            objectives: [
              { id: "o1", project_id: "p-mail", procedure: { title: "Triage" } },
              { id: "o2", project_id: "p-files", procedure: { title: "File" } },
              { id: "o3", project_id: "p-mail", procedure: { title: "Report" } },
            ],
          },
        },
      },
    }),
  );
  await page.route(`**/api/missions/${MID}/plan/save-as-plan`, async (r) => {
    saves.push(r.request().postDataJSON());
    await r.fulfill({ status: 201, json: { id: "plan-77e0b1aa", version: 1 } });
  });
  await page.goto(`/mission?m=${MID}`);
  await page.getByTestId("mission-overflow").click();
  await page.getByTestId("mission-save-as-plan").click();
  const dialog = page.getByTestId("save-as-plan-dialog");
  await expect(dialog).toBeVisible();
  // The page behind the dialog is inert, on a phone too (its menu sheet lifted its own inert).
  await expect.poll(() => page.evaluate(() => document.getElementById("root")?.hasAttribute("inert"))).toBe(true);
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => document.getElementById("root")?.hasAttribute("inert"))).toBe(true);
  // Focus moves INTO the dialog — onto the title once the plan is read — and the ⋯ menu is gone.
  await expect(page.getByTestId("save-as-plan-title-input")).toBeFocused();
  await expect(page.getByTestId("mission-overflow-menu")).toHaveCount(0);
  await expect(page.getByTestId("save-as-plan-title-input")).toHaveValue(""); // never the mission's title
  // Slots by first appearance: p1 is the mailbox (o1, o3), p2 the Nextcloud (o2).
  await expect(dialog).toContainText("now: Acme mailbox · objectives o1, o3");
  await expect(dialog).toContainText("now: Acme Nextcloud · objective o2");
  await page.getByTestId("save-as-plan-title-input").fill("Monthly invoice run");
  await page.getByTestId("save-as-plan-slot-p1").fill("Mail");
  await page.getByTestId("save-as-plan-slot-p2").fill("Files");
  const save = page.getByTestId("save-as-plan-save");
  // Measured at rest: the dialog deploys (desktop) or slides up (phone) on an animation.
  await animationsSettled(page);
  expect((await save.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  // The actions stay in reach while the sheet's body scrolls, and nothing shows below them.
  await dialog.evaluate((el) => (el.scrollTop = el.scrollHeight));
  const view = page.viewportSize()!;
  const saveBox = (await save.boundingBox())!;
  expect(saveBox.y + saveBox.height).toBeLessThanOrEqual(view.height);
  const below = await dialog.evaluate((el) => {
    const foot = el.querySelector("[data-testid='save-as-plan-save']")!.parentElement!.getBoundingClientRect();
    const dlg = el.getBoundingClientRect();
    return Math.round(dlg.bottom - foot.bottom);
  });
  // A phone's bottom sheet: the sticky footer reaches the sheet's floor (desktop keeps its padding).
  if (view.width <= 800) {
    expect(below).toBeLessThanOrEqual(1);
    // …and it is the sheet's footer, held in place while the body scrolls.
    const position = await save.evaluate((b) => getComputedStyle(b.parentElement!).position);
    expect(position).toBe("sticky");
    // The fade above it says more of the sheet continues below (the mockup review on #1924).
    const fade = await save.evaluate((b) => getComputedStyle(b.parentElement!, "::before").backgroundImage);
    expect(fade).toContain("linear-gradient");
  }
  await save.click();
  await expect(page.getByTestId("save-as-plan-saved")).toBeVisible();
  expect(saves).toEqual([{ revision: 3, title: "Monthly invoice run", summary: "", slot_labels: ["Mail", "Files"] }]);
  const sent = JSON.stringify(saves);
  for (const leak of ["Acme", "p-mail", "p-files", "/srv/mail"]) expect(sent).not.toContain(leak);
  await expect(page.getByRole("link", { name: "Open the mission plan" })).toHaveAttribute(
    "href",
    missionPlanPath("plan-77e0b1aa"),
  );
});

test("a mission without a plan document says so and offers nothing to save", async ({ page }) => {
  await commonMocks(page);
  await mockRoster(page);
  await mockMissions(page, {
    missions: missionList([missionRow({ id: MID, title: "Fresh mission", state: "draft" })]),
    mission: { ...MISSION, id: MID, title: "Fresh mission", state: "draft", events: [], events_next_seq: null },
  });
  await page.route(`**/api/missions/${MID}/plan/revisions/latest`, (r) =>
    r.fulfill({ json: { mission_id: MID, plan: null } }),
  );
  await page.goto(`/mission?m=${MID}`);
  await page.getByTestId("mission-overflow").click();
  await page.getByTestId("mission-save-as-plan").click();
  await expect(page.getByTestId("save-as-plan-none")).toBeVisible();
  await expect(page.getByTestId("save-as-plan-save")).toHaveCount(0);
  // Focus is INSIDE the dialog, never left on the ⋯ trigger behind the inert page (phone too).
  await expect
    .poll(() => page.evaluate(() => !!document.activeElement?.closest("[data-testid='save-as-plan-dialog']")))
    .toBe(true);
  await page.waitForTimeout(500); // past any closing sheet's restore frame
  expect(await page.evaluate(() => !!document.activeElement?.closest("[data-testid='save-as-plan-dialog']"))).toBe(true);
});

test("a card's Export sends exactly that plan, and an import shows the plan's counts and that it runs nothing", async ({ page }) => {
  const writes: { url: string; body: unknown }[] = [];
  await mockLibrary(page, writes);
  await page.goto(MISSION_PLANS_PATH);
  await page.getByTestId("mission-plan-card-plan-1a2b3c4d").getByRole("button", { name: "Export" }).click();
  const exp = page.getByTestId("share-export");
  await expect(exp.getByRole("region", { name: "Mission plans" })).toBeVisible();
  await expect(exp.getByTestId("share-export-summary")).toContainText("1 chosen");
  await expect(exp.getByText("What a mission plan carries.")).toBeVisible();
  const refused = exp.getByTestId("share-export-row-mission-plan:plan-0badf00d");
  await expect(refused).toContainText("holds one of your stored secrets");
  await expect(refused.locator("input")).toBeDisabled();
  await expect(exp).toContainText("The procedures travel exactly as written");
  const download = page.waitForEvent("download");
  await exp.getByRole("button", { name: "Download bundle" }).click();
  await download;
  expect(writes).toEqual([{ url: "/api/share/export", body: { items: [{ kind: "mission-plan", id: "plan-1a2b3c4d" }] } }]);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Import…" }).click();
  const imp = page.getByTestId("share-import");
  await imp.getByTestId("share-file").setInputFiles({
    name: "invoices.battlelab",
    mimeType: "application/x-tar",
    buffer: Buffer.from("bundle"),
  });
  await expect(imp).toContainText("3 objectives · 3 project slots · 8 steps · runs nothing");
  await expect(imp).toContainText("Mission plan");
});

test("a lost archive fence re-reads the list, so the next click carries the current revision", async ({ page }) => {
  const writes: { url: string; body: unknown }[] = [];
  await mockLibrary(page, writes);
  let lists = 0;
  let posts = 0;
  await page.route("**/api/mission-plans**", async (r) => {
    const url = new URL(r.request().url());
    if (r.request().method() === "POST") {
      posts += 1;
      return r.fulfill({ status: 409, json: { detail: "the mission plan changed since you loaded it" } });
    }
    if (url.pathname === "/api/mission-plans") lists += 1;
    return r.fulfill({ json: url.pathname === "/api/mission-plans" ? { plans: PLANS } : DETAIL });
  });
  await page.goto(MISSION_PLANS_PATH);
  await expect(page.locator("[data-testid^='mission-plan-card-']")).toHaveCount(2);
  const before = lists;
  await page.getByTestId("mission-plan-card-plan-1a2b3c4d").getByRole("button", { name: "Archive" }).click();
  await expect.poll(() => posts).toBe(1);
  await expect.poll(() => lists).toBeGreaterThan(before);
});

test("with every plan archived, the empty state says so and keeps the toggle", async ({ page }) => {
  const writes: { url: string; body: unknown }[] = [];
  await mockLibrary(page, writes);
  await page.route("**/api/mission-plans**", (r) =>
    r.fulfill({ json: { plans: PLANS.map((p) => ({ ...p, archived: true })) } }),
  );
  await page.goto(MISSION_PLANS_PATH);
  await expect(page.getByTestId("mission-plans-empty")).toContainText("Every mission plan is archived");
  await page.getByTestId("mission-plans-show-archived").click();
  await expect(page.locator("[data-testid^='mission-plan-card-']")).toHaveCount(3);
  await expect(page.getByTestId("mission-plans-empty")).toHaveCount(0);
});
