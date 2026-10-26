import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Locator, type Page, test } from "@playwright/test";
import { mockRoster, ROSTER } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { SPECIALIST_NEW_PATH, SPECIALISTS_PATH, specialistPath } from "../src/lib/routes";
import { agentPath } from "../src/routes/settingsTabs";

// Library → Specialists and Settings → Agents → Model grades (#1693 S1), in a real browser on the
// desktop AND phone projects: what breaks here is layout (a card or a select pushing a phone
// sideways, a control under 44 px) and the requests the page actually sends (a whole-map grade
// overwrite, a skill pinned at a stale revision). The backend is mocked from the manifest roster;
// agents are chosen by what the roster says they can do, never by name.

type Row = { id: string; kind?: string; present?: boolean; runtime?: string; tools?: { mcp?: string | null }; model_select?: { offered?: { id: string }[] } };
const AGENTS = (ROSTER.engines as Row[]).filter((e) => e.kind === "agent" && e.present);
/** A terminal agent that loads skills and offers models (the specialist's agent). */
const CAPABLE = AGENTS.find((e) => e.runtime === "pty" && e.tools?.mcp && (e.model_select?.offered?.length ?? 0) > 1)!;
/** A terminal agent with no tool channel: it cannot hold a specialist with skills. */
const NO_TOOLS = AGENTS.find((e) => e.runtime === "pty" && !e.tools?.mcp && (e.model_select?.offered?.length ?? 0) > 0)!;
const MODEL = CAPABLE.model_select!.offered![0].id;

const skill = (id: string, name: string, revision = `r-${id}-2`) => ({
  id,
  revision,
  files: { "SKILL.md": `---\nname: ${name}\ndescription: ${name}\n---\n` },
  editable: true,
});
const SKILLS = [skill("tdd", "Test-driven development"), skill("git-hygiene", "Git hygiene")];
const TEMPLATES = [
  { id: "intern", title: "Intern", summary: "Small chores.", grade: "fast", skills: [] },
  { id: "implementer", title: "Implementer", summary: "Builds to acceptance criteria.", grade: "strong", skills: ["tdd"] },
];
const version = (o: Record<string, unknown>) => ({
  version: 1, title: "Implementer", summary: "Builds a phase.", skills: [], engine: CAPABLE.id,
  model: MODEL, effort: null, capacity: 2, created_at: 1, ...o,
});
const SPECIALIST = {
  id: "implementer", revision: 3, archived: false, grade: "strong", live: 0,
  skills_missing: ["ledger-rules"], skills_outdated: ["tdd"],
  current: version({
    version: 2,
    skills: [{ id: "tdd", revision: "r-tdd-1" }, { id: "ledger-rules", revision: "r-x" }],
  }),
};
const ARCHIVED = { id: "retired", revision: 2, archived: true, grade: null, live: 0, skills_missing: [], skills_outdated: [], current: version({ title: "Retired" }) };
const UNGRADED = { ...SPECIALIST, id: "intern", grade: null, skills_missing: [], skills_outdated: [], current: version({ title: "Intern", skills: [] }) };

const FIXTURE = JSON.parse(readFileSync(resolve(process.cwd(), "src/test/roster.fixture.json"), "utf8"));
const CAP_ROW = FIXTURE.engines.find((e: { id: string }) => e.id === CAPABLE.id);
const DETAIL = {
  id: CAPABLE.id, label: CAP_ROW.label, publisher: "battlelab", version: "1", contract: 1,
  source: "in-tree", kind: "agent", runtime: "pty",
  binary: { name: CAPABLE.id, env_var: "X_BIN", search_paths: ["~/.local/bin"] },
  provenance: { state: "adopted", via: "search_paths", path: "/home/operator/.local/bin/agent", note: null },
  store: { root: "~/.agent", resolved: "/home/operator/.agent", layout: "x", read_only: true },
  launch: { resume: "flag", new: "pin-flag", admission: null },
  transcript: { kind: "x", strict: false }, usage: { source: "plan", kind: "x" },
  capabilities: CAP_ROW.capabilities, models: [], maintenance: [],
};

async function backend(
  page: Page,
  opts: {
    list?: "full" | "empty" | "error";
    posts?: unknown[];
    prefs?: unknown[];
    detailFails?: boolean;
    /** Hold each PATCH until this resolves (a slow server). */
    patchGate?: Promise<void>;
    /** Hold the detail GET until this resolves (a slow first load). */
    detailGate?: Promise<void>;
    /** Every /api/config read after the first fails. */
    configFailsAfterFirst?: boolean;
    rosterOverrides?: Record<string, Record<string, unknown>>;
    /** Hold the FIRST grade POST's response (computed when it committed) until this resolves. */
    firstGradeGate?: Promise<void>;
    /** Hold the FIRST grade POST before it commits until this resolves. */
    firstGradeCommitGate?: Promise<void>;
  } = {},
) {
  const list = opts.list ?? "full";
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  let configReads = 0;
  // The server's map, per agent, merged across saves.
  const gradesMap: Record<string, Record<string, string | null>> = { [CAPABLE.id]: { [MODEL]: "strong" } };
  let gradePosts = 0;
  let gradesRev = 1;
  await page.route("**/api/config", (r) =>
    opts.configFailsAfterFirst && configReads++ > 0
      ? r.fulfill({ status: 500, json: { detail: "boom" } })
      : r.fulfill({ json: { csrf: "x", new_session_engines: AGENTS.map((e) => e.id), terminal_backend: "ws",
      auth_mode: "none", model_grades: { [CAPABLE.id]: { [MODEL]: "strong" } }, model_grades_rev: 1 } }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }));
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }));
  await page.route("**/api/agents/usage", (r) =>
    r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }));
  await page.route("**/api/library/skills", (r) => r.fulfill({ json: { skills: SKILLS } }));
  await page.route(/\/api\/specialists$/, (r) => {
    if (r.request().method() === "POST") {
      opts.posts?.push(r.request().postDataJSON());
      return r.fulfill({ status: 201, json: { ...SPECIALIST, id: "new-one", versions: [] } });
    }
    if (list === "error")
      return r.fulfill({ status: 503, json: { detail: "The specialist store is unavailable: database is locked." } });
    return r.fulfill({ json: { specialists: list === "empty" ? [] : [SPECIALIST, UNGRADED, ARCHIVED], templates: TEMPLATES } });
  });
  // The server's side of the revision fence: every write bumps the revision, and a write that
  // carries a stale one is a 409, exactly as `specialists_store` answers.
  let cur = { ...SPECIALIST, versions: [] as unknown[] };
  await page.route(/\/api\/specialists\/implementer$/, async (r) => {
    if (r.request().method() === "GET" && opts.detailGate) await opts.detailGate;
    if (opts.detailFails) return r.fulfill({ status: 503, json: { detail: "the specialist store is unavailable: database is locked" } });
    if (r.request().method() === "PATCH") {
      const body = r.request().postDataJSON();
      if (opts.patchGate) await opts.patchGate;
      opts.posts?.push(body);
      if (body.revision !== cur.revision) return r.fulfill({ status: 409, json: { detail: "the specialist changed since you loaded it" } });
      cur = { ...cur, revision: cur.revision + 1, current: { ...body.draft, version: cur.current.version + 1, created_at: 2 } };
    }
    return r.fulfill({ json: cur });
  });
  await page.route(/\/api\/specialists\/implementer\/(archive|unarchive)$/, (r) => {
    const body = r.request().postDataJSON();
    opts.posts?.push({ url: r.request().url().split("/").pop(), ...body });
    if (body.revision !== cur.revision) return r.fulfill({ status: 409, json: { detail: "the specialist changed since you loaded it" } });
    cur = { ...cur, revision: cur.revision + 1, archived: r.request().url().endsWith("/archive") };
    return r.fulfill({ json: cur });
  });
  await page.route("**/api/prefs", async (r) => {
    if (r.request().method() !== "POST") return r.fulfill({ json: {} });
    const body = r.request().postDataJSON();
    opts.prefs?.push(body);
    const first = gradePosts++ === 0;
    if (first && opts.firstGradeCommitGate) await opts.firstGradeCommitGate;
    // The server commits, bumps the revision, and answers with the map it holds at THAT moment
    // (null clears a row); a held answer therefore carries an older map than a later save's.
    for (const [engine, row] of Object.entries(body.model_grades ?? {}) as [string, Record<string, string | null>][]) {
      const cur = (gradesMap[engine] = { ...(gradesMap[engine] ?? {}), ...row });
      for (const k of Object.keys(cur)) if (cur[k] === null) delete cur[k];
    }
    const answer = JSON.parse(JSON.stringify(gradesMap));
    const rev = ++gradesRev;
    if (first && opts.firstGradeGate) await opts.firstGradeGate;
    return r.fulfill({ json: { model_grades: answer, model_grades_rev: rev } });
  });
  await mockRoster(page, { overrides: opts.rosterOverrides as never });
  await page.route(/\/api\/engines\/[^/]+$/, (r) => {
    const id = decodeURIComponent(r.request().url().split("/").pop()!);
    const row = AGENTS.find((e) => e.id === id);
    return row
      ? r.fulfill({ json: { ...DETAIL, id, label: id } })
      : r.fulfill({ status: 404, json: { detail: "unknown engine" } });
  });
}

/** Every visible element inside `root` ends inside the viewport (the page scrolls on its own box). */
async function noHorizontalOverflow(root: Locator) {
  const worst = await root.evaluate((el) => {
    const vw = document.documentElement.clientWidth;
    let bad = "";
    for (const n of [el, ...el.querySelectorAll("*")]) {
      const r = n.getBoundingClientRect();
      if (r.width && r.height && r.right > vw + 1) bad ||= `${n.tagName} ${(n.textContent ?? "").slice(0, 40)} ${r.right}>${vw}`;
    }
    return bad;
  });
  expect(worst).toBe("");
}

test("the pool shows grades, ungraded, missing and outdated skills, and fits the viewport", async ({ page }) => {
  await backend(page);
  await page.goto(SPECIALISTS_PATH);
  const card = page.getByTestId("specialist-card-implementer");
  await expect(card.getByTestId("specialist-grade")).toHaveText("Specialist");
  await expect(card).toContainText("missing from your library");
  await expect(card).toContainText("changed in your library");
  await expect(page.getByTestId("specialist-card-intern").getByTestId("specialist-grade")).toHaveText("Ungraded");
  // No reservations before S4: no pool capacity line at all, rather than a made-up one.
  await expect(page.getByTestId("pool-capacity")).toHaveCount(0);
  await noHorizontalOverflow(page.getByTestId("specialists-page"));
  // Archived ones are hidden from the pool, never lost: one toggle shows them.
  await expect(page.getByTestId("specialist-card-retired")).toHaveCount(0);
  await page.getByTestId("specialists-show-archived").click();
  await expect(page.getByTestId("specialist-card-retired")).toContainText("archived");
  await expect(page.getByTestId("specialist-card-implementer")).toHaveCount(0);
});

test("an empty pool points at the templates; an unreadable one says why and retries", async ({ page }) => {
  await backend(page, { list: "empty" });
  await page.goto(SPECIALISTS_PATH);
  await expect(page.getByText("No specialists yet.")).toBeVisible();

  const page2 = await page.context().newPage();
  await backend(page2, { list: "error" });
  await page2.goto(SPECIALISTS_PATH);
  await expect(page2.getByRole("alert")).toContainText("database is locked");
  await expect(page2.getByRole("button", { name: "Try again" })).toBeVisible();
  await page2.close();
});

test("skills on an agent with no tool channel are refused before Save; a capable agent saves with current pins", async ({ page }) => {
  const posts: unknown[] = [];
  await backend(page, { posts });
  await page.goto(SPECIALIST_NEW_PATH);
  await page.getByRole("button", { name: /Implementer/ }).click();
  await expect(page.getByTestId("specialist-skill-tdd")).toBeVisible();

  await page.getByRole("combobox", { name: "Agent" }).selectOption(NO_TOOLS.id);
  await expect(page.getByTestId("skills-refused")).toBeVisible();
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Choose a model");
  const create = page.getByRole("button", { name: "Create specialist" });
  await expect(create).toBeDisabled();

  await page.getByRole("combobox", { name: "Agent" }).selectOption(CAPABLE.id);
  await page.getByRole("combobox", { name: "Model" }).selectOption(MODEL);
  await expect(page.getByTestId("skills-refused")).toHaveCount(0);
  // The grade is DERIVED from the operator's map, never chosen here.
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Specialist");
  for (const name of ["Agent", "Model"]) {
    const box = (await page.getByRole("combobox", { name }).boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await noHorizontalOverflow(page.getByTestId("specialist-editor"));
  await create.click();
  await expect(page).toHaveURL(new RegExp(`${specialistPath("new-one")}$`));
  expect(posts).toEqual([
    {
      title: "Implementer",
      summary: "Builds to acceptance criteria.",
      skills: [{ id: "tdd", revision: "r-tdd-2" }],
      engine: CAPABLE.id,
      model: MODEL,
      effort: null,
      capacity: 2,
    },
  ]);
});

test("an edit re-pins an outdated skill to the library's current revision and saves a new version", async ({ page }) => {
  const posts: unknown[] = [];
  await backend(page, { posts });
  await page.goto(specialistPath("implementer"));
  await expect(page.getByTestId("specialist-skill-tdd")).toContainText("saving pins the new version");
  await page.getByRole("button", { name: "Remove skill ledger-rules" }).click();
  await page.getByRole("button", { name: "Save as v3" }).click();
  await expect.poll(() => posts.length).toBe(1);
  const sent = posts[0] as { revision: number; draft: { skills: unknown[] } };
  expect(sent.revision).toBe(3);
  expect(sent.draft.skills).toEqual([{ id: "tdd", revision: "r-tdd-2" }]);

  // A SECOND save carries the revision the first one returned: never a stale 409.
  const capacity = page.getByRole("spinbutton", { name: "At most this many at once" });
  await capacity.fill("");
  await expect(page.getByTestId("specialist-problems")).toContainText("whole number");
  await capacity.fill("5");
  await page.getByRole("button", { name: "Save as v4" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect((posts[1] as { revision: number; draft: { capacity: number } }).revision).toBe(4);
  expect((posts[1] as { draft: { capacity: number } }).draft.capacity).toBe(5);
  await expect(page.getByRole("button", { name: "Save as v5" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("archive and unarchive from the editor, fenced on the current revision", async ({ page }) => {
  const posts: unknown[] = [];
  await backend(page, { posts });
  await page.goto(specialistPath("implementer"));
  await page.getByRole("button", { name: "Archive" }).click();
  await expect(page.getByTestId("specialist-archived")).toBeVisible();
  await expect(page.getByRole("button", { name: /Save as/ })).toBeDisabled();
  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByTestId("specialist-archived")).toHaveCount(0);
  expect(posts).toEqual([
    { url: "archive", revision: 3 },
    { url: "unarchive", revision: 4 },
  ]);
});

test("a detail that fails to load says why and can never be saved as a new specialist", async ({ page }) => {
  const posts: unknown[] = [];
  await backend(page, { posts, detailFails: true });
  await page.goto(specialistPath("implementer"));
  await expect(page.getByTestId("specialist-load-error")).toContainText("database is locked");
  // Nothing loaded means nothing to edit: the form stays locked and Save stays off.
  await expect(page.getByRole("textbox", { name: "Title" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /Save as/ })).toBeDisabled();
  expect(posts).toEqual([]);
});

test("Model grades saves one row as a patch, and No grade clears it", async ({ page }) => {
  const prefs: unknown[] = [];
  await backend(page, { prefs });
  await page.goto(agentPath(CAPABLE.id));
  const table = page.getByTestId("model-grades");
  await expect(table).toBeVisible();
  const first = table.getByRole("combobox", { name: `Grade for ${MODEL}` });
  await expect(first).toHaveValue("strong");
  expect((await first.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await first.selectOption("frontier");
  await expect.poll(() => prefs.length).toBe(1);
  await first.selectOption("");
  await expect.poll(() => prefs.length).toBe(2);
  expect(prefs).toEqual([
    { model_grades: { [CAPABLE.id]: { [MODEL]: "frontier" } } },
    { model_grades: { [CAPABLE.id]: { [MODEL]: null } } },
  ]);
  await noHorizontalOverflow(table);
});

test("the form is locked while a save is in flight, so nothing typed meanwhile is lost", async ({ page }) => {
  let release!: () => void;
  const patchGate = new Promise<void>((r) => (release = r));
  await backend(page, { patchGate });
  await page.goto(specialistPath("implementer"));
  const title = page.getByRole("textbox", { name: "Title" });
  await expect(title).toHaveValue("Implementer");
  await page.getByRole("button", { name: "Remove skill ledger-rules" }).click();
  await page.getByRole("button", { name: "Save as v3" }).click();
  await expect(title).toBeDisabled();
  release();
  await expect(title).toBeEnabled();
  await expect(page.getByRole("button", { name: "Save as v4" })).toBeVisible();
});

test("a grade whose config reload fails shows what the server saved and says so", async ({ page }) => {
  await backend(page, { configFailsAfterFirst: true });
  await page.goto(agentPath(CAPABLE.id));
  const first = page.getByTestId("model-grades").getByRole("combobox", { name: `Grade for ${MODEL}` });
  await expect(first).toHaveValue("strong");
  await first.selectOption("frontier");
  await expect(page.getByTestId("model-grades").getByRole("alert")).toContainText("couldn’t reload");
  await expect(first).toHaveValue("frontier"); // never snaps back to the stale config
});

test("an agent that cannot start a new session is not offered", async ({ page }) => {
  await backend(page, { rosterOverrides: { [NO_TOOLS.id]: { status: "retiring" } } });
  await page.goto(SPECIALIST_NEW_PATH);
  const agent = page.getByRole("combobox", { name: "Agent" });
  await expect(agent.locator(`option[value="${CAPABLE.id}"]`)).toHaveCount(1);
  await expect(agent.locator(`option[value="${NO_TOOLS.id}"]`)).toHaveCount(0);
});

const MODEL_2 = CAPABLE.model_select!.offered![1].id;

/** Client-side navigation: the app (and what it holds in memory) stays loaded. */
async function navigate(page: Page, path: string) {
  await page.evaluate((p) => {
    history.pushState({}, "", p);
    dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

test("the form stays locked until the specialist loads, so nothing typed early is overwritten", async ({ page }) => {
  let release!: () => void;
  const detailGate = new Promise<void>((r) => (release = r));
  await backend(page, { detailGate });
  await page.goto(specialistPath("implementer"));
  const title = page.getByRole("textbox", { name: "Title" });
  await expect(title).toBeDisabled();
  release();
  await expect(title).toBeEnabled();
  await expect(title).toHaveValue("Implementer");
});

test("archive and unarchive keep unsaved edits in the form", async ({ page }) => {
  const posts: unknown[] = [];
  await backend(page, { posts });
  await page.goto(specialistPath("implementer"));
  const title = page.getByRole("textbox", { name: "Title" });
  await expect(title).toHaveValue("Implementer");
  await title.fill("Builder");
  await page.getByRole("button", { name: "Archive" }).click();
  await expect(page.getByTestId("specialist-archived")).toBeVisible();
  await expect(title).toHaveValue("Builder");
  await page.getByRole("button", { name: "Unarchive" }).click();
  await expect(page.getByTestId("specialist-archived")).toHaveCount(0);
  await expect(title).toHaveValue("Builder");
  await page.getByRole("button", { name: "Remove skill ledger-rules" }).click();
  await page.getByRole("button", { name: "Save as v3" }).click();
  await expect.poll(() => posts.length).toBe(3);
  const save = posts[2] as { revision: number; draft: { title: string } };
  expect(save.revision).toBe(5); // fenced on the revision archive/unarchive returned
  expect(save.draft.title).toBe("Builder");
});

test("two grade saves during a reload outage both stay, in the table and in the editor", async ({ page }) => {
  await backend(page, { configFailsAfterFirst: true });
  await page.goto(agentPath(CAPABLE.id));
  const table = page.getByTestId("model-grades");
  const a = table.getByRole("combobox", { name: `Grade for ${MODEL}` });
  const b = table.getByRole("combobox", { name: `Grade for ${MODEL_2}` });
  await expect(a).toHaveValue("strong");
  await a.selectOption("frontier");
  await expect(table.getByRole("alert")).toBeVisible();
  await expect(a).toBeEnabled();
  await b.selectOption("fast");
  await expect(b).toBeEnabled();
  await expect(a).toHaveValue("frontier"); // the first save never snaps back
  await expect(b).toHaveValue("fast");
  // The editor reads the same confirmed map: the specialist on MODEL is now Senior.
  await navigate(page, specialistPath("implementer"));
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Senior");
});

test("overlapping grade saves on two agents, answered out of order during a reload outage, both stay", async ({ page }) => {
  let release!: () => void;
  const firstGradeGate = new Promise<void>((r) => (release = r));
  await backend(page, { configFailsAfterFirst: true, firstGradeGate });
  await page.goto(agentPath(CAPABLE.id));
  // Save A on the first agent; the server commits it, its response is held.
  const a = page.getByTestId("model-grades").getByRole("combobox", { name: `Grade for ${MODEL}` });
  await expect(a).toHaveValue("strong");
  await a.selectOption("frontier");
  // Leave for another agent and save B there; its response (A + B) arrives first.
  const other = NO_TOOLS.model_select!.offered![0].id;
  await navigate(page, agentPath(NO_TOOLS.id));
  const b = page.getByTestId("model-grades").getByRole("combobox", { name: `Grade for ${other}` });
  await expect(b).toHaveValue("");
  await b.selectOption("fast");
  await expect(page.getByTestId("model-grades").getByRole("alert")).toBeVisible();
  // Now A's OLDER answer (A only) lands. It must not roll B back.
  release();
  await page.waitForTimeout(300);
  await expect(b).toHaveValue("fast");
  await navigate(page, agentPath(CAPABLE.id));
  await expect(
    page.getByTestId("model-grades").getByRole("combobox", { name: `Grade for ${MODEL}` }),
  ).toHaveValue("frontier");
  await navigate(page, specialistPath("implementer"));
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Senior");
});

/** Save A on one model, leave and come back (a fresh grade table), then save B on the SAME model. */
async function sameRowTwice(page: Page, a: string, b: string) {
  await page.goto(agentPath(CAPABLE.id));
  const row = () => page.getByTestId("model-grades").getByRole("combobox", { name: `Grade for ${MODEL}` });
  await expect(row()).toHaveValue("strong");
  await row().selectOption(a);
  await navigate(page, agentPath(NO_TOOLS.id));
  await expect(page.getByTestId("model-grades")).toBeVisible();
  await navigate(page, agentPath(CAPABLE.id));
  await expect(row()).toBeEnabled();
  await row().selectOption(b);
  await expect(page.getByTestId("model-grades").getByRole("alert")).toBeVisible();
  return row;
}

test("same row saved twice: the save that COMMITS last wins, even when it was started first", async ({ page }) => {
  let release!: () => void;
  const firstGradeCommitGate = new Promise<void>((r) => (release = r));
  await backend(page, { configFailsAfterFirst: true, firstGradeCommitGate });
  // "frontier" is held before its commit; "fast" commits and answers first.
  const row = await sameRowTwice(page, "frontier", "fast");
  await expect(row()).toHaveValue("fast");
  release(); // "frontier" now commits LAST: the server holds frontier
  await expect(row()).toHaveValue("frontier");
  await navigate(page, specialistPath("implementer"));
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Senior");
});

test("same row saved twice: an older commit's late answer never replaces a newer one", async ({ page }) => {
  let release!: () => void;
  const firstGradeGate = new Promise<void>((r) => (release = r));
  await backend(page, { configFailsAfterFirst: true, firstGradeGate });
  // "frontier" commits first but its answer is held; "fast" commits after it and answers.
  const row = await sameRowTwice(page, "frontier", "fast");
  await expect(row()).toHaveValue("fast");
  release(); // the older answer (frontier) lands late: the server holds fast
  await page.waitForTimeout(300);
  await expect(row()).toHaveValue("fast");
  await navigate(page, specialistPath("implementer"));
  await expect(page.getByTestId("specialist-grade-line")).toContainText("Intern");
});
