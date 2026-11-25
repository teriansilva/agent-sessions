import { expect, test, type Page } from "@playwright/test";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// #2338: the sidebar filters the session list by custom tag. Picking a tag in the Filters block
// sends `tag=` to `/api/sessions` and the list shows what the server answered; "All tags" lifts
// it. Runs on both projects: on a phone the sidebar is the off-canvas drawer, and the tag select
// sits on its own row so it keeps the 44px touch floor and a readable width.

const row = (id: string, title: string, tag: string) => ({
  id: `claude:${id}`,
  engine: "claude",
  uuid: id,
  short_uuid: id.slice(0, 8),
  cwd: "/x",
  project: { kind: "folder", id: "/x", name: "/x" },
  last_mtime: 1,
  first_user_message: title,
  title,
  sticky: false,
  archived: false,
  tag,
});
const ALL = [
  row("aaaaaaaa-0000-0000-0000-000000000001", "Fix the login bug", "bug"),
  row("aaaaaaaa-0000-0000-0000-000000000002", "Cut the release", "release"),
  row("aaaaaaaa-0000-0000-0000-000000000003", "Untagged chore", ""),
];

async function setup(page: Page, project: string): Promise<(string | null)[]> {
  const tagsSeen: (string | null)[] = [];
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
  await page.route("**/api/sessions?**", (r) => {
    const tag = new URL(r.request().url()).searchParams.get("tag");
    tagsSeen.push(tag);
    const sessions = tag ? ALL.filter((s) => s.tag.toLowerCase() === tag.toLowerCase()) : ALL;
    return r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: {
          projects: [],
          engines: ["claude"],
          tags: [
            { tag: "bug", key: "bug", count: 1 },
            { tag: "release", key: "release", count: 1 },
          ],
        },
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
  return tagsSeen;
}

test("picking a tag narrows the session list to it; All tags lifts it (#2338)", async ({
  page,
}, testInfo) => {
  const tagsSeen = await setup(page, testInfo.project.name);
  const sidebar = page.locator("aside.sidebar");
  await expect(sidebar.getByText("Untagged chore")).toBeVisible();

  const select = sidebar.getByLabel("Filter by tag");
  await expect(select).toBeVisible();
  // Usable on its own row: not squeezed beside the agent / mission selects.
  const box = await select.boundingBox();
  expect(box!.width).toBeGreaterThan(150);
  if (testInfo.project.name === "mobile") expect(box!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);

  await select.selectOption("bug");
  await expect.poll(() => tagsSeen.at(-1)).toBe("bug");
  await expect(sidebar.getByText("Fix the login bug")).toBeVisible();
  await expect(sidebar.getByText("Cut the release")).toHaveCount(0);
  await expect(sidebar.getByText("Untagged chore")).toHaveCount(0);
  await expect(page.getByTestId("filters-toggle")).toContainText("1 filter");

  await select.selectOption("");
  await expect.poll(() => tagsSeen.at(-1)).toBeNull();
  await expect(sidebar.getByText("Untagged chore")).toBeVisible();
});
