/** Session clusters in the sidebar (#1997 P2), in a real browser on both projects.
 *
 * The mocks are STATEFUL: a rename, a move or a turned-off switch changes what later reads
 * answer, so a poll that lands mid-test agrees with the change instead of reverting it. What is
 * pinned here is what jsdom cannot show: the tree and the head are laid out inside the real
 * sidebar (a grid column on desktop, the drawer on a phone), every control meets the 44 px floor
 * under a coarse pointer, the picker is a real modal over the drawer, and nothing overflows 390 px.
 */
import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { mockNightjarShell } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const TC_LOGIN = "tc_0a1b2c3d4e5f";
const TC_BILL = "tc_112233445566";
const PROJECT = { kind: "project", id: "p-a", name: "alpha", color: "#f59e0b" };

interface Row {
  n: string;
  title: string;
  cluster: string | null;
  mission?: boolean;
}

interface State {
  enabled: boolean;
  titles: Record<string, { title: string; kept: boolean }>;
  rows: Row[];
  writes: { method: string; path: string; body: unknown }[];
  queries: URLSearchParams[];
  /** Extra, empty projects after alpha: a long tree (#2187). */
  extra?: number;
}

function fresh(): State {
  return {
    enabled: true,
    titles: {
      [TC_LOGIN]: { title: "Login flow", kept: false },
      [TC_BILL]: { title: "Billing", kept: false },
    },
    rows: [
      { n: "a1", title: "Fix the login redirect", cluster: TC_LOGIN },
      { n: "a2", title: "Session cookie expiry", cluster: TC_LOGIN },
      { n: "b1", title: "Invoice PDF totals", cluster: TC_BILL },
      { n: "b2", title: "Stripe webhook retries", cluster: TC_BILL },
      { n: "c1", title: "Bump vitest", cluster: null },
      { n: "m1", title: "Changelog and tag", cluster: null, mission: true },
    ],
    writes: [],
    queries: [],
  };
}

const MISSION = { id: "msn_" + "a".repeat(32), title: "Release v0.21", state: "running" };
const uuid = (n: string) => `${n}000000-1111-2222-3333-444444444444`.slice(0, 36);

function ref(s: State, id: string | null) {
  return id && s.titles[id] ? { id, ...s.titles[id] } : null;
}

function sessionRow(s: State, r: Row) {
  return {
    id: `claude:${uuid(r.n)}`,
    engine: "claude",
    uuid: uuid(r.n),
    short_uuid: r.n,
    cwd: "/home/u/alpha",
    project: PROJECT,
    last_mtime: 1_700_000_000,
    first_user_message: "",
    title: r.title,
    sticky: false,
    archived: false,
    mission: r.mission ? MISSION : null,
    cluster: s.enabled && !r.mission ? ref(s, r.cluster) : null,
  };
}

function facets(s: State) {
  const count = (id: string) => s.rows.filter((r) => r.cluster === id && !r.mission).length;
  return {
    projects: [
      { ...PROJECT, count: s.rows.length },
      ...Array.from({ length: s.extra ?? 0 }, (_, i) => ({
        kind: "project",
        id: `p-x${i}`,
        name: `project-${String(i).padStart(2, "0")}`,
        color: "#64748b",
        count: 0,
      })),
    ],
    engines: ["claude"],
    missions: [{ ...MISSION, count: 1, by_project: { "p-a": 1 } }],
    no_mission: s.rows.length - 1,
    ...(s.enabled
      ? {
          clusters: Object.keys(s.titles).map((id) => ({
            ...ref(s, id)!,
            project_id: "p-a",
            count: count(id),
          })),
        }
      : {}),
  };
}

async function setup(page: Page, project: string, s: State = fresh()): Promise<State> {
  // The shared mocked shell first; the routes below are registered later, so they win.
  await mockNightjarShell(page);
  const clusters = () => ({
    enabled: s.enabled,
    agent_tool: true,
    interval_minutes: 30,
    configured: true,
  });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        username: null,
        onboarded: true,
        overview_expanded: [],
        projects_hidden: [],
        pulse: { configured: true },
        clusters: clusters(),
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    const q = new URL(r.request().url()).searchParams;
    s.queries.push(q);
    const cluster = q.get("cluster");
    const mission = q.get("mission");
    const rows = s.rows.filter((row) => {
      if (mission) return !!row.mission;
      if (cluster === "none") return !row.cluster && !row.mission;
      if (cluster) return row.cluster === cluster && !row.mission;
      return true;
    });
    return r.fulfill({
      json: {
        sessions: rows.map((row) => sessionRow(s, row)),
        next_offset: null,
        total: rows.length,
        live_total: 0,
        facets: facets(s),
      },
    });
  });
  await page.route(/\/api\/clusters(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        clusters: Object.keys(s.titles).map((id) => ({
          ...ref(s, id)!,
          ai_title: s.titles[id].title,
          source: "ai",
          project_id: "p-a",
          count: s.rows.filter((row) => row.cluster === id).length,
          summary: id === TC_LOGIN ? "Two sessions on sign-in and its cookie." : "Invoices and webhooks.",
        })),
        sweeps: {},
        unreadable: false,
        settings: clusters(),
      },
    }),
  );
  await page.route(/\/api\/clusters\/tc_[0-9a-f]{12}$/, (r) => {
    const id = r.request().url().split("/").pop()!;
    const body = r.request().postDataJSON() as { title?: string; keep?: boolean };
    s.writes.push({ method: r.request().method(), path: `/api/clusters/${id}`, body });
    if (typeof body?.title === "string") s.titles[id] = { title: body.title, kept: true };
    if (typeof body?.keep === "boolean") s.titles[id].kept = body.keep;
    return r.fulfill({ json: ref(s, id) });
  });
  await page.route(/\/api\/sessions\/[^/]+\/cluster$/, (r) => {
    const body = r.request().postDataJSON() as { cluster_id: string | null };
    const key = decodeURIComponent(r.request().url().split("/").at(-2)!);
    s.writes.push({ method: r.request().method(), path: `session:${key}`, body });
    const row = s.rows.find((x) => `claude:${uuid(x.n)}` === key)!;
    if (body.cluster_id) row.cluster = body.cluster_id;
    return r.fulfill({ json: { id: key, cluster: ref(s, body.cluster_id) } });
  });

  await page.goto(NEW_SESSION_PATH);
  if (project === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
  await expect(page.getByText("Fix the login redirect")).toBeVisible();
  return s;
}

const sidebar = (page: Page) => page.locator("aside.sidebar");
const rowTitles = (page: Page) =>
  sidebar(page).locator("ul[aria-label$='sessions'] > li a").evaluateAll((links) =>
    links.map((a) => a.querySelector("div[title]")?.getAttribute("title") ?? ""),
  );

async function selectCluster(page: Page, name: RegExp) {
  await sidebar(page).getByRole("button", { name: "Unfold alpha" }).click();
  await sidebar(page).getByTestId("cluster-node").filter({ hasText: name }).click();
}

test("selecting a cluster filters the list to its sessions and shows its head", async ({
  page,
}, testInfo) => {
  const s = await setup(page, testInfo.project.name);
  // Every row carries its cluster before anything is selected.
  await expect(sidebar(page).getByTestId("row-cluster-tag")).toHaveCount(4);
  await selectCluster(page, /Login flow/);
  await expect
    .poll(async () => (await rowTitles(page)).sort())
    .toEqual(["Fix the login redirect", "Session cookie expiry"]);
  expect(s.queries.at(-1)!.get("cluster")).toBe(TC_LOGIN);
  expect(s.queries.at(-1)!.get("project")).toBe("p-a");
  const head = sidebar(page).getByTestId("cluster-head");
  await expect(head.getByTestId("cluster-head-title")).toHaveText("Login flow");
  await expect(head.getByTestId("cluster-head-tag")).toHaveText(/AI title/i);
  await expect(head).toContainText("Two sessions on sign-in and its cookie.");
  // The tree marks it, and the mission and the rest are still one press away.
  await expect(
    sidebar(page).getByTestId("cluster-node").filter({ hasText: "Login flow" }),
  ).toHaveAttribute("aria-current", "true");
  await sidebar(page).getByTestId("cluster-none-node").click();
  await expect.poll(() => rowTitles(page)).toEqual(["Bump vitest"]);
  await sidebar(page).getByTestId("cluster-mission-node").click();
  await expect.poll(() => rowTitles(page)).toEqual(["Changelog and tag"]);
  expect(s.queries.at(-1)!.get("mission")).toBe(MISSION.id);
  expect(s.queries.at(-1)!.get("cluster")).toBeNull();
});

test("renaming a cluster keeps the title; Let HQ rename hands it back", async ({
  page,
}, testInfo) => {
  const s = await setup(page, testInfo.project.name);
  await selectCluster(page, /Login flow/);
  const head = sidebar(page).getByTestId("cluster-head");
  await head.getByRole("button", { name: "Rename" }).click();
  const input = head.getByRole("textbox", { name: "Cluster title" });
  await expect(input).toBeFocused();
  await input.fill("Auth and sessions");
  await input.press("Enter");
  await expect(head.getByTestId("cluster-head-title")).toHaveText("Auth and sessions");
  await expect(head.getByTestId("cluster-head-tag")).toHaveText(/Kept · your title/i);
  expect(s.writes).toContainEqual({
    method: "PATCH",
    path: `/api/clusters/${TC_LOGIN}`,
    body: { title: "Auth and sessions" },
  });
  // The tree and the rows' chips follow from the server's next read, not a local patch.
  await expect(
    sidebar(page).getByTestId("cluster-node").filter({ hasText: "Auth and sessions" }),
  ).toContainText(/Kept/i);
  await expect(sidebar(page).getByTestId("row-cluster-tag").first()).toHaveText("Auth and sessions");
  await head.getByRole("button", { name: "Let HQ rename" }).click();
  await expect(head.getByTestId("cluster-head-tag")).toHaveText(/AI title/i);
  expect(s.writes.at(-1)).toEqual({
    method: "PATCH",
    path: `/api/clusters/${TC_LOGIN}`,
    body: { keep: false },
  });
});

test("Move to cluster… moves a session and its row follows", async ({ page }, testInfo) => {
  const s = await setup(page, testInfo.project.name);
  const row = sidebar(page).locator("li", { hasText: "Bump vitest" });
  await expect(row.getByTestId("row-cluster-tag")).toHaveCount(0);
  await row.getByRole("button", { name: "Session actions" }).click();
  await page.getByRole("menuitem", { name: "Move session to a cluster" }).click();
  const dialog = page.getByRole("dialog", { name: "Move to cluster" });
  await expect(dialog).toBeVisible();
  // A real modal over the sidebar: it is on top at its own centre, and Tab stays inside it.
  const box = (await dialog.boundingBox())!;
  const onTop = await page.evaluate(
    ([x, y]) => !!document.elementFromPoint(x, y)?.closest("[role='dialog']"),
    [box.x + box.width / 2, box.y + 24],
  );
  expect(onTop).toBe(true);
  await expect(dialog.getByRole("button", { name: /Login flow/ })).toBeFocused();
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => !!document.activeElement?.closest("[role='dialog']"))).toBe(
      true,
    );
  }
  await dialog.getByRole("button", { name: /Billing/ }).click();
  await expect(dialog).toBeHidden();
  expect(s.writes).toContainEqual({
    method: "PUT",
    path: `session:claude:${uuid("c1")}`,
    body: { cluster_id: TC_BILL },
  });
  await expect(row.getByTestId("row-cluster-tag")).toHaveText("Billing");
  // The mission-held session is never offered the move: the mission is its group.
  await sidebar(page)
    .locator("li", { hasText: "Changelog and tag" })
    .getByRole("button", { name: "Session actions" })
    .click();
  await expect(page.getByRole("menu")).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Move session to a cluster" })).toHaveCount(0);
});

test("the tree, the head and the picker meet the touch floor and fit the sidebar", async ({
  page,
}, testInfo) => {
  await setup(page, testInfo.project.name);
  await selectCluster(page, /Login flow/);
  const head = sidebar(page).getByTestId("cluster-head");
  await expect(head).toBeVisible();
  const coarse = await page.evaluate(
    () => matchMedia("(pointer: coarse)").matches || matchMedia("(max-width: 800px)").matches,
  );
  const controls = [
    sidebar(page).getByTestId("filters-toggle"),
    sidebar(page).getByRole("button", { name: "Fold alpha" }),
    sidebar(page).getByTestId("cluster-node").first(),
    sidebar(page).getByTestId("cluster-none-node"),
    head.getByRole("button", { name: "Rename" }),
    head.getByRole("button", { name: "Keep title" }),
    head.getByTestId("cluster-head-menu"),
  ];
  for (const c of controls) {
    await c.scrollIntoViewIfNeeded();
    const b = (await c.boundingBox())!;
    // The head's buttons are 44 px at every width; the tree is compact only under a mouse.
    const floor = coarse ? TOUCH_FLOOR : 24;
    expect(b.height, await c.evaluate((el) => el.outerHTML.slice(0, 80))).toBeGreaterThanOrEqual(floor);
    if (coarse) expect(b.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  for (const name of ["Rename", "Keep title"]) {
    const b = (await head.getByRole("button", { name }).boundingBox())!;
    expect(b.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  // Nothing in the tree or the head is wider than the sidebar that holds it.
  const overflow = await sidebar(page).evaluate((aside) => {
    const max = aside.getBoundingClientRect().right + 0.5;
    return [...aside.querySelectorAll("[data-testid='project-tree'] *, [data-testid='cluster-head'] *")]
      .filter((el) => el.getBoundingClientRect().right > max)
      .map((el) => el.outerHTML.slice(0, 60));
  });
  expect(overflow).toEqual([]);
  // The list is still reachable under the tree and the head.
  const last = sidebar(page).locator("ul[aria-label$='sessions'] > li").last();
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  // The rename editor's own controls hold the floor too.
  await head.getByRole("button", { name: "Rename" }).click();
  for (const name of ["Save cluster title", "Cancel cluster rename"]) {
    const b = (await head.getByRole("button", { name }).boundingBox())!;
    expect(b.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(b.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await page.keyboard.press("Escape");
  await expect(head.getByRole("textbox", { name: "Cluster title" })).toHaveCount(0);
});

test("with clusters turned off the tree lists projects with no clusters, and there is no chip or menu item", async ({
  page,
}, testInfo) => {
  const s = fresh();
  s.enabled = false;
  await setup(page, testInfo.project.name, s);
  // The tree is the project filter (#2187): it stays, only the clusters in it go.
  const tree = sidebar(page).getByTestId("project-tree");
  await expect(tree).toBeVisible();
  await expect(sidebar(page).getByLabel("Filter by project")).toHaveCount(0);
  await expect(tree.getByTestId("cluster-node")).toHaveCount(0);
  await expect(tree.getByTestId("cluster-none-node")).toHaveCount(0);
  await expect(sidebar(page).getByTestId("row-cluster-tag")).toHaveCount(0);
  await sidebar(page)
    .locator("li", { hasText: "Bump vitest" })
    .getByRole("button", { name: "Session actions" })
    .click();
  await expect(page.getByRole("menu")).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Move session to a cluster" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // A project still filters the list, and still lists its missions.
  await tree.getByRole("button", { name: /^alpha/ }).click();
  await expect.poll(() => s.queries.at(-1)?.get("project")).toBe("p-a");
  await expect(tree.getByTestId("cluster-mission-node")).toBeVisible();
  await tree.getByTestId("cluster-mission-node").click();
  await expect.poll(() => s.queries.at(-1)?.get("mission")).toBe(MISSION.id);
  await expect(sidebar(page).getByTestId("filters-scope")).toHaveText(`alpha › ${MISSION.title}`);
});

test("a long tree scrolls to its last project, which a keyboard selects; folded, the chip keeps the whole scope (#2187)", async ({
  page,
}, testInfo) => {
  const s = fresh();
  s.extra = 30;
  const st = await setup(page, testInfo.project.name, s);
  const tree = sidebar(page).getByTestId("project-tree");
  const last = tree.getByRole("button", { name: /^project-29/ });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
  const coarse = await page.evaluate(
    () => matchMedia("(pointer: coarse)").matches || matchMedia("(max-width: 800px)").matches,
  );
  expect((await last.boundingBox())!.height).toBeGreaterThanOrEqual(coarse ? TOUCH_FLOOR : 24);
  // A keyboard reaches and presses it like any node.
  await last.focus();
  await expect(last).toBeFocused();
  await page.keyboard.press("Enter");
  await expect.poll(() => st.queries.at(-1)?.get("project")).toBe("p-x29");
  await expect(last).toHaveAttribute("aria-current", "true");
  // The list below the tree is still reachable.
  await tree.getByRole("button", { name: /^All sessions/ }).click();
  const row = sidebar(page).locator("ul[aria-label$='sessions'] > li").last();
  await row.scrollIntoViewIfNeeded();
  await expect(row).toBeInViewport();

  // A long scope, folded: the chip gives way, never past the header, and the whole scope stays
  // in the toggle's accessible name (the ellipsis is CSS, not a cut string).
  await sidebar(page).getByTestId("filters-toggle").scrollIntoViewIfNeeded();
  await selectCluster(page, /Login flow/);
  const toggle = sidebar(page).getByTestId("filters-toggle");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  const chip = sidebar(page).getByTestId("filters-scope");
  await expect(chip).toHaveText("alpha › Login flow");
  await expect(toggle).toHaveAccessibleName(/alpha › Login flow/);
  const [c, t] = [(await chip.boundingBox())!, (await toggle.boundingBox())!];
  expect(c.x + c.width).toBeLessThanOrEqual(t.x + t.width + 0.5);
  await expect(toggle).toContainText("Active");
  expect((await toggle.boundingBox())!.height).toBeGreaterThanOrEqual(coarse ? TOUCH_FLOOR : 30);
});
