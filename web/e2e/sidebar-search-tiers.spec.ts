import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";

// #2373: a text search answers in groups (title, then details, then the conversation), each row
// with where it matched and a snippet whose matched words are marked; a subagent's hit names it;
// words in other messages are counted; a credential-shaped match keeps its row but withholds the
// snippet; and a note says when the search could not look everywhere. Runs on both projects
// (desktop column, mobile drawer): real browser, because the groups, the marks and reaching the
// last result are layout facts jsdom cannot prove.

const uuid = (n: number) => `0000000${n}-0000-4000-8000-000000000000`;

function row(n: number, title: string, match?: Record<string, unknown>) {
  return {
    id: `claude:${uuid(n)}`,
    engine: "claude",
    uuid: uuid(n),
    short_uuid: String(n),
    cwd: "/home/u/arcade",
    project: { kind: "folder", id: "/home/u/arcade", name: "/home/u/arcade" },
    last_mtime: 1_700_000_000 - n,
    first_user_message: title,
    title,
    sticky: false,
    archived: false,
    working: false,
    intervention_required: false,
    child_count: 0,
    descendant_count: 0,
    rollup: null,
    attention_count: 0,
    attention: [],
    ...(match ? { match } : {}),
  };
}

const RESULTS = [
  row(1, "Add Hungarian quiz packs", { tier: "title", field: "title" }),
  row(2, "Language support for all games", {
    tier: "details",
    field: "recap",
    snippet: "…mockups list Romanian and Hungarian next…",
    marks: [[27, 33]],
  }),
  row(3, "Continued: research party games", {
    tier: "conversation",
    field: "you",
    snippet: "Add romanian, hungarian, polish as languages",
    marks: [[14, 20]],
  }),
  row(4, "Localise the word games", {
    tier: "conversation",
    field: "subagent",
    child: "string audit",
    snippet: "…the Hungarian plural forms break the counter…",
    marks: [[5, 11]],
    more: 1,
  }),
  row(5, "Rotate the API key", { tier: "conversation", field: "you", withheld: true }),
];

async function setup(page: Page, project: string): Promise<void> {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  await page.route("**/api/sessions**", (r) => {
    const q = new URL(r.request().url()).searchParams.get("q");
    const searching = !!q;
    return r.fulfill({
      json: {
        // Without a search the rows carry no `match` (undefined is dropped from the JSON).
        sessions: searching ? RESULTS : RESULTS.map((r) => ({ ...r, match: undefined })),
        next_offset: null,
        total: RESULTS.length,
        facets: { projects: [], engines: ["claude"] },
        ...(searching
          ? { search: { mode: "index", indexing: { indexed: 1240, total: 3480 } } }
          : {}),
      },
    });
  });
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.goto(NEW_SESSION_PATH);
  if (project === "mobile") {
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

test("a search answers in groups with marked snippets, provenance and a note (#2373)", async ({
  page,
}, testInfo) => {
  await setup(page, testInfo.project.name);
  const sidebar = page.locator("aside.sidebar");
  await sidebar.getByRole("searchbox", { name: "Search sessions" }).fill("hungar");

  // The groups, in the server's order, each opened once.
  const tiers = sidebar.getByTestId("search-tier");
  await expect(tiers).toHaveCount(3);
  await expect(tiers.nth(0)).toContainText("Title");
  await expect(tiers.nth(1)).toContainText("In the details");
  await expect(tiers.nth(2)).toContainText("Found in the conversation");
  await expect(tiers.nth(2)).toContainText("3");

  // Snippets: where they matched, the matched word marked, a subagent named, words elsewhere.
  const snippets = sidebar.getByTestId("row-match");
  await expect(snippets).toHaveCount(4); // the title hit has no snippet line
  await expect(snippets.nth(0)).toContainText("recap");
  await expect(snippets.nth(0).locator("mark")).toHaveText("Hungar");
  await expect(snippets.nth(2)).toContainText("subagent");
  await expect(snippets.nth(2)).toContainText("string audit");
  await expect(snippets.nth(2)).toContainText("+ 1 more word elsewhere in this session");
  await expect(snippets.nth(3)).toContainText("snippet withheld");
  await expect(snippets.nth(3).locator("mark")).toHaveCount(0);

  // A mark is visibly highlighted (not transparent) in this theme.
  const bg = await snippets
    .nth(0)
    .locator("mark")
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");

  // The note says the search could not look everywhere yet.
  await expect(sidebar.getByTestId("search-note")).toContainText("Still indexing conversations");

  // The last result can be reached and is a real link (keyboard and touch alike).
  const last = sidebar.getByRole("link", { name: /Rotate the API key/ });
  await last.scrollIntoViewIfNeeded();
  await expect(last).toBeInViewport();
});
