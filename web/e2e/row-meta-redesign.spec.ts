import { expect, test, type Page } from "@playwright/test";

// #508: real-browser proof for the session-row redesign — folder chip dropped, time spelled
// out, favorite moved into the ⋯ menu and shown as a ★ prefix on favorited rows. Runs on both
// the desktop and mobile Playwright projects (the sidebar is a grid column on desktop, an
// off-canvas drawer on mobile). Red on the pre-#508 row (folder chip + "10m ago" + standalone
// star button); green after.

const now = Math.floor(Date.now() / 1000);
const sessions = [
  {
    id: "claude:pinned",
    engine: "claude",
    uuid: "pinned",
    short_uuid: "pinned",
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now, // "just now"
    first_user_message: "",
    title: "Pinned session",
    sticky: true,
    archived: false,
    working: false,
  },
  {
    id: "claude:active",
    engine: "claude",
    uuid: "active",
    short_uuid: "active",
    cwd: "/home/u/work/api",
    project: { kind: "folder", id: "/home/u/work/api", name: "api" },
    last_mtime: now - 630, // "10 mins ago"
    first_user_message: "",
    title: "Active work",
    sticky: false,
    archived: false,
    working: false,
  },
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
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions,
        next_offset: null,
        total: sessions.length,
        facets: {
          projects: [{ kind: "folder", id: "/home/u/proj", name: "/home/u/proj" }],
          engines: ["claude"],
        },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions/*/favorite", (r) =>
    r.fulfill({ json: { id: "claude:active", sticky: true } }),
  );
  await page.route("**/api/sessions/*/unfavorite", (r) =>
    r.fulfill({ json: { id: "claude:pinned", sticky: false } }),
  );

  await page.goto("/");
  // Desktop renders the sidebar as a grid column; mobile hides it behind the drawer toggle.
  if (project === "mobile") {
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

test.describe("session row redesign (#508)", () => {
  test("drops the folder chip, spells out the time, and shows ★ on favorited rows", async ({
    page,
  }, testInfo) => {
    await setup(page, testInfo.project.name);
    const sidebar = page.locator("aside.sidebar");
    await expect(sidebar.getByText("Pinned session")).toBeVisible();

    // Folder chip gone: no decorative ▸ marker, no cwd path in the sidebar.
    await expect(sidebar.getByText("▸")).toHaveCount(0);
    await expect(sidebar.getByText("~/work/api")).toHaveCount(0);
    await expect(sidebar.getByText("~/proj")).toHaveCount(0);

    // Time spelled out — "10 mins ago", never the compact "10m ago".
    await expect(sidebar.getByText(/\d+m ago/)).toHaveCount(0);
    await expect(sidebar.getByText("10 mins ago")).toBeVisible();

    // The favorited row leads with the amber ★; the unfavorited one doesn't.
    const pinnedRow = page.getByRole("listitem").filter({ hasText: "Pinned session" });
    const activeRow = page.getByRole("listitem").filter({ hasText: "Active work" });
    await expect(pinnedRow.locator('[title="Favorited"]')).toBeVisible();
    await expect(activeRow.locator('[title="Favorited"]')).toHaveCount(0);

    // Favorite is no longer a standalone row button (it moved into the ⋯ menu).
    await expect(pinnedRow.getByRole("button", { name: "Unfavorite" })).toHaveCount(0);
    await expect(activeRow.getByRole("button", { name: "Favorite" })).toHaveCount(0);
  });

  test("favoriting from the ⋯ menu adds the ★ prefix to the row", async ({ page }, testInfo) => {
    await setup(page, testInfo.project.name);
    const activeRow = page.getByRole("listitem").filter({ hasText: "Active work" });
    await expect(activeRow.locator('[title="Favorited"]')).toHaveCount(0);

    await activeRow.hover();
    await activeRow.getByRole("button", { name: "Session actions" }).click();
    const menu = page.getByRole("menu", { name: "Session actions" });
    await expect(menu).toBeVisible();
    await menu.getByRole("menuitem", { name: "Favorite session" }).click();

    // The row now carries the ★ prefix.
    await expect(activeRow.locator('[title="Favorited"]')).toBeVisible();
  });
});
