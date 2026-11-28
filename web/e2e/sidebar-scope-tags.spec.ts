/** The project/cluster field as a multiselect tag field (#2438), in a real browser on both projects.
 *
 * The mock answers `/api/sessions` with the server's rule: the project/cluster picks are ONE facet
 * whose values OR together, and a chosen cluster narrows its own project only. What is pinned here
 * is what jsdom cannot show: the chips wrap inside the real sidebar (a column on desktop, the
 * drawer on a phone), focus stays in the combobox while picking, every chip's × meets the 44 px
 * floor under a coarse pointer — a long-label chip among many included — the input does not make a
 * phone zoom, nothing overflows 390 px, and with the keyboard up the active option stays in view.
 */
import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { mockNightjarShell } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const P = {
  a: { kind: "project", id: "p-a", name: "Sophia", color: "#4aa3ff" },
  b: { kind: "project", id: "p-b", name: "chrononote", color: "#c879ff" },
  c: { kind: "project", id: "p-c", name: "infra", color: "#ff9f43" },
  // A long PROJECT name: a cluster chip's breadcrumb must truncate, never push its × out.
  d: {
    kind: "project",
    id: "p-d",
    name: "Quarterly customer-facing billing platform migration workstream",
    color: "#2ed573",
  },
};
const LONG =
  "Customer-facing invoice export and reconciliation rework for the Q4 close";
const CLUSTERS = [
  {
    id: "tc_aaaaaaaaaaa1",
    title: "API review",
    kept: false,
    project_id: "p-a",
  },
  {
    id: "tc_bbbbbbbbbbb1",
    title: "Shadow pilot",
    kept: false,
    project_id: "p-b",
  },
  { id: "tc_ccccccccccc1", title: LONG, kept: false, project_id: "p-c" },
  { id: "tc_ddddddddddd1", title: "Ledger", kept: false, project_id: "p-d" },
];
const ROWS = [
  {
    n: "a1",
    title: "Review the export API",
    p: "a",
    cluster: "tc_aaaaaaaaaaa1",
  },
  { n: "a2", title: "Refactor the invoice exporter", p: "a", cluster: null },
  {
    n: "b1",
    title: "Shadow pilot: compare note diffs",
    p: "b",
    cluster: "tc_bbbbbbbbbbb1",
  },
  { n: "b2", title: "Sync engine retries", p: "b", cluster: null },
  {
    n: "c1",
    title: "Rotate the runner certificates",
    p: "c",
    cluster: "tc_ccccccccccc1",
  },
  {
    n: "d1",
    title: "Reconcile the ledger exports",
    p: "d",
    cluster: "tc_ddddddddddd1",
  },
] as const;
const uuid = (n: string) =>
  `${n}000000-1111-2222-3333-444444444444`.slice(0, 36);

async function setup(page: Page, project: string): Promise<URLSearchParams[]> {
  const queries: URLSearchParams[] = [];
  await mockNightjarShell(page);
  const clusters = {
    enabled: true,
    agent_tool: true,
    interval_minutes: 30,
    configured: true,
  };
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
        clusters,
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    const q = new URL(r.request().url()).searchParams;
    queries.push(q);
    const ps = new Set(q.getAll("project"));
    const cs = new Set(q.getAll("cluster"));
    const parents = new Set(
      CLUSTERS.filter((c) => cs.has(c.id)).map((c) => c.project_id),
    );
    const narrowed = [...ps].filter((p) => !parents.has(p));
    const rows = ROWS.filter(
      (row) =>
        (!ps.size && !cs.size) ||
        (row.cluster !== null && cs.has(row.cluster)) ||
        narrowed.includes(P[row.p].id),
    );
    return r.fulfill({
      json: {
        sessions: rows.map((row) => ({
          id: `claude:${uuid(row.n)}`,
          engine: "claude",
          uuid: uuid(row.n),
          short_uuid: row.n,
          cwd: `/home/u/${P[row.p].name}`,
          project: P[row.p],
          last_mtime: 1_700_000_000,
          first_user_message: "",
          title: row.title,
          sticky: false,
          archived: false,
          mission: null,
          cluster: row.cluster
            ? CLUSTERS.find((c) => c.id === row.cluster)
            : null,
        })),
        next_offset: null,
        total: rows.length,
        live_total: 0,
        facets: {
          projects: Object.values(P).map((p) => ({
            ...p,
            count: ROWS.filter((row) => P[row.p].id === p.id).length,
          })),
          engines: ["claude"],
          missions: [],
          no_mission: ROWS.length,
          clusters: CLUSTERS.map((c) => ({
            ...c,
            count: ROWS.filter((row) => row.cluster === c.id).length,
          })),
        },
      },
    });
  });
  await page.route(/\/api\/clusters(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        clusters: CLUSTERS.map((c) => ({
          ...c,
          ai_title: c.title,
          source: "ai",
          count: 1,
          summary: "Sessions on one topic.",
        })),
        sweeps: {},
        unreadable: false,
        settings: clusters,
      },
    }),
  );
  await page.goto(NEW_SESSION_PATH);
  if (project === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
  await expect(page.getByText("Rotate the runner certificates")).toBeVisible();
  return queries;
}

const sidebar = (page: Page) => page.locator("aside.sidebar");
// The tag field's own options (the sidebar's native <select>s have options too).
const suggestions = (page: Page) =>
  sidebar(page)
    .getByRole("listbox", { name: "Projects and clusters" })
    .getByRole("option");
const projectNode = (page: Page, name: string) =>
  sidebar(page).getByTestId("project-node").filter({ hasText: name });

test("two projects picked: both chips, one request with both, both listed; × removes one (#2438)", async ({
  page,
}, testInfo) => {
  const queries = await setup(page, testInfo.project.name);
  const sb = sidebar(page);
  await projectNode(page, "Sophia").click();
  await projectNode(page, "chrononote").click();
  await expect
    .poll(() => queries.at(-1)?.getAll("project"))
    .toEqual(["p-a", "p-b"]);
  await expect(sb.getByTestId("scope-chip")).toHaveText([
    "Sophia",
    "chrononote",
  ]);
  await expect(projectNode(page, "Sophia")).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(sb.getByText("Refactor the invoice exporter")).toBeVisible();
  await expect(sb.getByText("Sync engine retries")).toBeVisible();
  await expect(sb.getByText("Rotate the runner certificates")).toHaveCount(0);

  await sb.getByRole("button", { name: "Remove chrononote" }).click();
  await expect.poll(() => queries.at(-1)?.getAll("project")).toEqual(["p-a"]);
  await expect(sb.getByText("Sync engine retries")).toHaveCount(0);
  await expect(sb.getByTestId("scope-chip")).toHaveText(["Sophia"]);
});

test("a cluster picked by keyboard: focus stays, its head shows; another project hides the head (#2438)", async ({
  page,
}, testInfo) => {
  const queries = await setup(page, testInfo.project.name);
  const sb = sidebar(page);
  const input = sb.getByRole("combobox", { name: "Add a project or cluster" });
  await input.click();
  await input.pressSequentially("API");
  // Typing asks the server nothing.
  const asked = queries.length;
  await expect(suggestions(page)).toHaveText([/Sophia ›\s*API review/]);
  expect(queries.length).toBe(asked);
  await input.press("Enter");
  await expect
    .poll(() => queries.at(-1)?.getAll("cluster"))
    .toEqual(["tc_aaaaaaaaaaa1"]);
  await expect(input).toBeFocused();
  await expect(sb.getByTestId("scope-chip")).toHaveText([
    /Sophia ›\s*API review/,
  ]);
  await expect(page.getByTestId("cluster-head")).toBeVisible();
  await expect(sb.getByText("Review the export API")).toBeVisible();
  await expect(sb.getByText("Refactor the invoice exporter")).toHaveCount(0);

  // OR within the field: an unrelated project joins the cluster; the head is for one cluster only.
  await projectNode(page, "infra").click();
  await expect.poll(() => queries.at(-1)?.getAll("project")).toEqual(["p-c"]);
  expect(queries.at(-1)?.getAll("cluster")).toEqual(["tc_aaaaaaaaaaa1"]);
  await expect(page.getByTestId("cluster-head")).toHaveCount(0);
  await expect(sb.getByText("Rotate the runner certificates")).toBeVisible();
  await expect(sb.getByText("Review the export API")).toBeVisible();
});

test("on a phone every × is 44×44 and inside the field, long chips included; the input does not zoom; nothing overflows (#2438)", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "mobile",
    "the touch floor is the phone's",
  );
  await setup(page, testInfo.project.name);
  const sb = sidebar(page);
  // Many chips: a project, and clusters of OTHER projects (a cluster of a picked project would
  // narrow it, replacing its chip), one with a long title and one under a long project name.
  await projectNode(page, "Sophia").click();
  const input = sb.getByRole("combobox", { name: "Add a project or cluster" });
  for (const q of ["Shadow", "reconciliation", "Ledger"]) {
    await input.click();
    await input.pressSequentially(q);
    await suggestions(page).first().click();
  }
  await expect(sb.getByTestId("scope-chip")).toHaveCount(4);
  const removes = sb.getByRole("button", { name: /^Remove / });
  await expect(removes).toHaveCount(4);
  const field = sb.getByRole("group", { name: "Projects & clusters" });
  const fieldBox = (await field.boundingBox())!;
  for (const box of await removes.evaluateAll((els) =>
    els
      .map((e) => e.getBoundingClientRect())
      .map((b) => ({ w: b.width, h: b.height, left: b.left, right: b.right })),
  )) {
    expect(box.w).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.h).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    // Inside the field: a long breadcrumb never pushes the × out (#2438 review).
    expect(box.left).toBeGreaterThanOrEqual(fieldBox.x);
    expect(box.right).toBeLessThanOrEqual(fieldBox.x + fieldBox.width + 0.5);
  }
  const inputBox = await input.evaluate((e) => ({
    h: e.getBoundingClientRect().height,
    fs: parseFloat(getComputedStyle(e).fontSize),
  }));
  expect(inputBox.h).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(inputBox.fs).toBeGreaterThanOrEqual(16);
  // The long chip truncates its label, never its ×, and nothing spills sideways.
  const overflow = await page.evaluate(() => {
    const aside = document.querySelector("aside.sidebar")!;
    return {
      doc:
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
      aside: aside.scrollWidth - aside.clientWidth,
    };
  });
  expect(overflow.doc).toBeLessThanOrEqual(0);
  expect(overflow.aside).toBeLessThanOrEqual(0);

  // Keyboard up: a short viewport. The active option stays in view as the arrows move it.
  await page.setViewportSize({ width: 390, height: 420 });
  await input.click();
  for (let i = 0; i < 4; i++) await input.press("ArrowDown");
  const activeId = await input.getAttribute("aria-activedescendant");
  expect(activeId).toBeTruthy();
  await expect(page.locator(`[id="${activeId}"]`)).toBeInViewport();
});
