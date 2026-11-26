/** Subagent sessions in the sidebar (#2325), in a real browser on both projects.
 *
 * jsdom cannot show what this pins: the disclosure chip and the subagent controls meet the 44 px
 * floor where a finger is the pointer, subagent rows are laid out INDENTED under their main
 * session inside the real sidebar (a grid column on desktop, the drawer on a phone), nothing
 * overflows 390 px, and real clicks reach the chip, the attention strip and the archive prompt.
 * The mocks are stateful: the list's 15 s poll agrees with every change a test makes.
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { mockNightjarShell } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";

const uuid = (n: string) => `${n.padEnd(8, "0")}-1111-2222-3333-${"4".repeat(12)}`.slice(0, 36);
const key = (n: string) => `claude:${uuid(n)}`;

interface Node {
  n: string;
  title: string;
  parent?: string;
  needs?: boolean;
  working?: boolean;
  archived?: boolean;
}

interface State {
  nodes: Node[];
  /** Answer the next children read with this status instead (error/slow states). */
  childrenFail: number;
  childrenDelayMs: number;
  archiveCalls: string[][];
}

function row(s: State, node: Node) {
  const kids = s.nodes.filter((x) => x.parent === node.n && !x.archived);
  const desc: Node[] = [];
  const stack = [...kids];
  while (stack.length) {
    const d = stack.shift()!;
    desc.push(d);
    stack.push(...s.nodes.filter((x) => x.parent === d.n && !x.archived));
  }
  const att = desc.filter((d) => d.needs);
  const rollup = att.length
    ? "attention"
    : desc.some((d) => d.working)
      ? "working"
      : desc.length
        ? "idle"
        : null;
  return {
    id: key(node.n),
    engine: "claude",
    uuid: uuid(node.n),
    short_uuid: node.n,
    cwd: "/home/u/alpha",
    project: { kind: "folder", id: "/home/u/alpha", name: "/home/u/alpha" },
    last_mtime: 1_700_000_000,
    first_user_message: node.title,
    title: node.title,
    sticky: false,
    archived: false,
    working: !!node.working,
    intervention_required: !!node.needs,
    intervention_reason: node.needs ? "approve a command" : null,
    child_count: kids.length,
    descendant_count: desc.length,
    rollup,
    attention_count: att.length,
    attention: att.slice(0, 10).map((d) => key(d.n)),
    ...(node.parent ? { parent_key: key(node.parent), lineage_source: "process" } : {}),
  };
}

function pathOf(s: State, node: Node, main: string) {
  const out: { id: string; title: string }[] = [];
  let p = node.parent;
  while (p) {
    const pn = s.nodes.find((x) => x.n === p)!;
    out.unshift({ id: key(pn.n), title: pn.title });
    if (pn.n === main) break;
    p = pn.parent;
  }
  return out;
}

function paged<T>(items: T[], url: URL, cap = 200) {
  const offset = Number(url.searchParams.get("offset") ?? 0);
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 50), cap);
  return {
    sessions: items.slice(offset, offset + limit),
    total: items.length,
    next_offset: offset + limit < items.length ? offset + limit : null,
  };
}

async function setup(
  page: Page,
  project: string,
  nodes: Node[],
  opts: { mode?: "hidden" } = {},
): Promise<State> {
  // A copy: tests mutate their nodes (archive), and the fixtures are shared module constants.
  const s: State = {
    nodes: nodes.map((n) => ({ ...n })),
    childrenFail: 0,
    childrenDelayMs: 0,
    archiveCalls: [],
  };
  await mockNightjarShell(page);
  if (opts.mode) {
    // The display mode is a Settings choice (not a sidebar control): store it before the app boots.
    await page.addInitScript((m) => localStorage.setItem("tr-sidebar-subagents", m), opts.mode);
  }
  await page.route(/\/api\/sessions(\?.*)?$/, (r) => {
    const url = new URL(r.request().url());
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const mains = s.nodes.filter((x) => !x.parent && !x.archived);
    const kept = mains.filter((m) => !q || m.title.toLowerCase().includes(q));
    const outside = mains.filter((m) => !kept.includes(m) && row(s, m).attention_count > 0);
    return r.fulfill({
      json: {
        sessions: kept.map((m) => row(s, m)),
        next_offset: null,
        total: kept.length,
        live_total: 0,
        facets: { projects: [], engines: ["claude"] },
        ...(q
          ? {
              attention_outside: {
                count: outside.reduce((n, m) => n + row(s, m).attention_count, 0),
                mains: outside.map((m) => ({
                  id: key(m.n),
                  engine: "claude",
                  title: m.title,
                  attention_count: row(s, m).attention_count,
                })),
              },
            }
          : {}),
      },
    });
  });
  const lineage = async (r: Route, kind: "children" | "attention") => {
    const url = new URL(r.request().url());
    const id = decodeURIComponent(url.pathname.split("/").at(-2)!);
    const main = s.nodes.find((x) => key(x.n) === id)!;
    if (kind === "children") {
      if (s.childrenDelayMs) await new Promise((res) => setTimeout(res, s.childrenDelayMs));
      if (s.childrenFail > 0) {
        s.childrenFail -= 1;
        return r.fulfill({ status: 500, json: { detail: "boom" } });
      }
      const kids = s.nodes.filter((x) => x.parent === main.n && !x.archived);
      return r.fulfill({ json: paged(kids.map((k) => row(s, k)), url) });
    }
    const desc: Node[] = [];
    const walk = (p: string) => {
      for (const k of s.nodes.filter((x) => x.parent === p && !x.archived)) {
        desc.push(k);
        walk(k.n);
      }
    };
    walk(main.n);
    const att = desc.filter((d) => d.needs).map((d) => ({ ...row(s, d), path: pathOf(s, d, main.n) }));
    // Pages of 10 here, so the "+N more need you" path is exercised with a dozen rows.
    return r.fulfill({ json: paged(att, url, 10) });
  };
  await page.route(/\/api\/sessions\/[^/]+\/children\?/, (r) => lineage(r, "children"));
  await page.route(/\/api\/sessions\/[^/]+\/attention\?/, (r) => lineage(r, "attention"));
  await page.route(/\/api\/sessions\/[^/]+\/archive-with-subagents$/, (r) => {
    const id = decodeURIComponent(r.request().url().split("/").at(-2)!);
    const subs = (r.request().postDataJSON() as { subagents: string[] }).subagents;
    s.archiveCalls.push([id, ...subs]);
    const results = [id, ...subs].map((k, i) => {
      if (i === subs.length) return { id: k, result: "failed", reason: "teardown failed" };
      s.nodes.find((x) => key(x.n) === k)!.archived = true;
      return { id: k, result: "archived", reason: null };
    });
    return r.fulfill({ json: { id, results } });
  });

  await openSidebar(page, project);
  return s;
}

async function openSidebar(page: Page, project: string) {
  await page.goto(NEW_SESSION_PATH);
  if (project === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

const sidebar = (page: Page) => page.locator("aside.sidebar");
const mainLink = (page: Page, n: string) => sidebar(page).locator(`a[href="/s/claude/${uuid(n)}"]`).first();

async function noOverflow(page: Page) {
  const over = await sidebar(page).evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(over).toBeLessThanOrEqual(1);
}

const TREE: Node[] = [
  { n: "m1", title: "Ship the federation fix" },
  { n: "k1", title: "opencode run e2e suite", parent: "m1", working: true },
  { n: "k2", title: "codex security review", parent: "m1" },
  { n: "m2", title: "Notification volume audit" },
];

test("subagents stay collapsed until the chip opens them, indented under their main session", async ({
  page,
}, info) => {
  await setup(page, info.project.name, TREE);
  await expect(mainLink(page, "m1")).toBeVisible();
  await expect(sidebar(page).getByText("opencode run e2e suite")).toHaveCount(0);
  await expect(
    sidebar(page).getByRole("button", { name: /show 2 subagents, working/i }),
  ).toBeVisible();
  const chip = sidebar(page).getByTestId("subagent-toggle");
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  const box = (await chip.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await chip.click();
  await expect(chip).toHaveAttribute("aria-expanded", "true");
  await expect(chip).toHaveAccessibleName(/hide 2 subagents/i);
  const child = mainLink(page, "k1");
  await expect(child).toBeVisible();
  const mainBox = (await mainLink(page, "m1").boundingBox())!;
  const childBox = (await child.boundingBox())!;
  expect(childBox.x).toBeGreaterThan(mainBox.x + 10); // indented, in the same column
  expect(childBox.y).toBeGreaterThan(mainBox.y);
  // The other main session follows the group, not the other way round.
  const otherBox = (await mainLink(page, "m2").boundingBox())!;
  expect(otherBox.y).toBeGreaterThan(childBox.y);
  await noOverflow(page);
  await chip.click();
  await expect(mainLink(page, "k1")).toHaveCount(0);
});

const NEEDY: Node[] = [
  { n: "m1", title: "Ship the federation fix" },
  { n: "k1", title: "explore web src", parent: "m1" },
  { n: "g1", title: "lint pass grandchild", parent: "k1", needs: true },
  { n: "k2", title: "codex security review", parent: "m1" },
];

test("a needs-you grandchild opens its main session in nested mode", async ({ page }, info) => {
  await setup(page, info.project.name, NEEDY);
  const grand = mainLink(page, "g1");
  await expect(grand).toBeVisible();
  await expect(sidebar(page).getByTestId("row-crumb").first()).toHaveText("↳ explore web src");
  await expect(sidebar(page).getByRole("img", { name: "a subagent needs you" }).first()).toBeVisible();
  await noOverflow(page);
});

test("the subagent display is chosen in Settings, not in the sidebar; hidden keeps the needs-you row", async ({
  page,
}, info) => {
  await setup(page, info.project.name, NEEDY);
  await expect(mainLink(page, "g1")).toBeVisible();
  // The sidebar carries no mode control: it is a rarely-changed, device-local Settings choice.
  await expect(sidebar(page).getByTestId("subagent-mode")).toHaveCount(0);
  await expect(sidebar(page).getByRole("radio", { name: /hidden/i })).toHaveCount(0);

  await page.goto(settingsPath("appearance"));
  const group = page.getByRole("radiogroup", { name: "Subagent sessions" });
  await expect(group.getByRole("radio", { name: /Nested/ })).toHaveAttribute("aria-checked", "true");
  const hidden = group.getByRole("radio", { name: /Hidden/ });
  expect((await hidden.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await hidden.click();
  await expect(hidden).toHaveAttribute("aria-checked", "true");
  expect(await page.evaluate(() => localStorage.getItem("tr-sidebar-subagents"))).toBe("hidden");

  // Hidden: no other subagent rows, the attention one still shows.
  await openSidebar(page, info.project.name);
  await expect(mainLink(page, "m1")).toBeVisible();
  await expect(mainLink(page, "g1")).toBeVisible();
  await expect(mainLink(page, "k2")).toHaveCount(0);
  await expect(sidebar(page).getByTestId("subagent-count")).toHaveText("2");
  await noOverflow(page);
});

test("a filtered-out main session's needs-you subagent is one tap away from the strip", async ({
  page,
}, info) => {
  await setup(page, info.project.name, [...NEEDY, { n: "m2", title: "Notification volume audit" }]);
  await sidebar(page).getByLabel("Search sessions").fill("notification");
  await expect(mainLink(page, "m1")).toHaveCount(0);
  const strip = sidebar(page).getByTestId("attention-outside");
  await expect(strip).toContainText("1 session needs you outside this filter");
  const show = strip.getByRole("button", { name: /outside this filter/ });
  expect((await show.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await show.click();
  const link = strip.locator(`a[href="/s/claude/${uuid("g1")}"]`);
  await expect(link).toBeVisible();
  await expect(strip.getByTestId("row-crumb")).toHaveText(
    "↳ Ship the federation fix › explore web src",
  );
  // A subagent itself never matched the search.
  await expect(sidebar(page).locator("ul[aria-label='1 sessions'] > li")).toHaveCount(1);
  await noOverflow(page);
});

test("twelve needs-you subagents page through '+N more need you'", async ({ page }, info) => {
  const kids: Node[] = Array.from({ length: 12 }, (_, i) => ({
    n: `n${String(i).padStart(2, "0")}`,
    title: `needs you ${i}`,
    parent: "m1",
    needs: true,
  }));
  // Hidden mode lists only the attention rows, so the page boundary is visible.
  await setup(page, info.project.name, [{ n: "m1", title: "Ship the federation fix" }, ...kids], {
    mode: "hidden",
  });
  await expect(sidebar(page).getByText("needs you 9")).toBeVisible();
  await expect(sidebar(page).getByTestId("subagent-row")).toHaveCount(10);
  const more = sidebar(page).getByRole("button", { name: "+2 more need you" });
  expect((await more.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await more.click();
  await expect(sidebar(page).getByText("needs you 11")).toBeVisible();
  await expect(sidebar(page).getByTestId("subagent-row")).toHaveCount(12);
});

test("three hundred subagents load lazily, one page at a time", async ({ page }, info) => {
  const kids: Node[] = Array.from({ length: 300 }, (_, i) => ({
    n: `c${String(i).padStart(3, "0")}`,
    title: `child ${i}`,
    parent: "m1",
  }));
  await setup(page, info.project.name, [{ n: "m1", title: "Ship the federation fix" }, ...kids]);
  await sidebar(page).getByRole("button", { name: /show 300 subagents/i }).click();
  await expect(sidebar(page).getByTestId("subagent-row")).toHaveCount(50);
  const more = sidebar(page).getByRole("button", { name: "More subagents (250)" });
  expect((await more.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await more.click();
  await expect(sidebar(page).getByTestId("subagent-row")).toHaveCount(100);
});

test("the lazy read shows loading, then an error with a working retry", async ({ page }, info) => {
  const s = await setup(page, info.project.name, TREE);
  s.childrenDelayMs = 600;
  s.childrenFail = 1;
  await sidebar(page).getByRole("button", { name: /show 2 subagents/i }).click();
  await expect(sidebar(page).getByText("Loading subagents…")).toBeVisible();
  const alert = sidebar(page).getByRole("alert");
  await expect(alert).toContainText("Couldn't load subagents.");
  s.childrenDelayMs = 0;
  const retry = alert.getByRole("button", { name: "Retry" });
  expect((await retry.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await retry.click();
  await expect(mainLink(page, "k1")).toBeVisible();
});

test("an empty children read says so", async ({ page }, info) => {
  const s = await setup(page, info.project.name, TREE);
  // Both subagents archived elsewhere after the list was read.
  for (const n of s.nodes) if (n.parent) n.archived = true;
  await sidebar(page).getByRole("button", { name: /show 2 subagents/i }).click();
  await expect(sidebar(page).getByTestId("subagent-empty")).toBeVisible();
});

test("archive asks first and reports a partial failure per key", async ({ page }, info) => {
  const s = await setup(page, info.project.name, TREE);
  const rowWrap = sidebar(page)
    .locator("li", { has: page.locator(`a[href="/s/claude/${uuid("m1")}"]`) })
    .first();
  await rowWrap.getByRole("button", { name: "Session actions" }).click();
  await page.getByRole("menuitem", { name: /archive session/i }).click();
  const dialog = page.getByTestId("archive-subagents-dialog");
  await expect(dialog).toBeVisible();
  const withSubs = dialog.getByRole("button", { name: "Archive with 2 subagents" });
  await expect(withSubs).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Archive only this" })).toBeVisible();
  await withSubs.click();
  expect(s.archiveCalls).toEqual([[key("m1"), key("k1"), key("k2")]]);
  const report = page.getByTestId("archive-subagents-result");
  await expect(report).toContainText("2 of 3 done");
  await expect(report).toContainText("failed · teardown failed");
  await report.getByRole("button", { name: "Done" }).click();
  await expect(mainLink(page, "m1")).toHaveCount(0);
});
