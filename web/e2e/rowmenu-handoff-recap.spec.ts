import { expect, test, type Page } from "@playwright/test";

// #597 follow-up: the terminal header's "Session brief" (Recap) and "Hand off…" are mirrored
// into the sidebar row's ⋯ context menu, so both are reachable without opening the session
// first. Real-browser proof on BOTH the desktop and mobile Playwright projects (the sidebar is
// a grid column on desktop, an off-canvas drawer on mobile). Red→green gate: neither menu item
// exists on origin/main, so the menuitem click target is absent before the change and present
// after. The modals themselves are the same ones the header mounts (own suites in
// handoff.spec.ts / session-recap.spec.ts); here we prove the menu wiring end-to-end.

const ENGINE = "claude";
const UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race";
const RECAP = [
  "Root-caused intermittent 401s to a token-refresh race.",
  "Added a single-flight lock + regression test (red then green).",
].join("\n");
const PREVIEW =
  "# Handoff — continued from a claude session\n\n[user] run the auth tests\n[agent] 4 passed";

const ROW = {
  id: `${ENGINE}:${UUID}`,
  engine: ENGINE,
  uuid: UUID,
  short_uuid: "aaaaaaaa",
  cwd: "/home/u/proj",
  project: { kind: "folder", id: "/home/u/proj", name: "proj" },
  last_mtime: 1_700_000_000,
  first_user_message: "",
  title: TITLE,
  sticky: false,
  archived: false,
  working: false,
  ai_summary: "Refactoring the token-refresh path to remove a double-refresh race.",
  ai_recap: RECAP,
  reviewed_at: 1_700_000_000,
  review_excluded: false,
  has_draft: false,
};

const ENGINES = [
  { id: "claude", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/claude" },
  { id: "codex", present: true, supports_new: true, supports_seed_start: true, seed_reason: null, bin: "/bin/codex" },
  { id: "gemini", present: true, supports_new: true, supports_seed_start: false, seed_reason: "no seed-capable start yet", bin: "/bin/gemini" },
  { id: "shell", present: true, supports_new: true, supports_seed_start: false, seed_reason: "not an agent engine", bin: "/bin/bash" },
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
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [ROW],
        next_offset: null,
        total: 1,
        facets: { projects: [ROW.project], engines: [ENGINE] },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: ENGINES } }));
  await page.route("**/api/handoff/prepare", (r) =>
    r.fulfill({
      json: {
        handle: "h-menu-1",
        preview: PREVIEW,
        meta: { mode: "quick", turns: 2, bytes: PREVIEW.length, cap: 8192 },
      },
    }),
  );

  await page.goto("/");
  // Desktop renders the sidebar as a grid column; mobile hides it behind the drawer toggle.
  if (project === "mobile") {
    await page.locator("header .navToggle").click();
  }
  await expect(page.locator("aside.sidebar")).toBeVisible();
}

async function openRowMenu(page: Page) {
  const row = page.getByRole("listitem").filter({ hasText: TITLE });
  await expect(row).toBeVisible();
  await row.hover(); // desktop reveals the ⋯ on hover; mobile shows it always
  await row.getByRole("button", { name: "Session actions" }).click();
  const menu = page.getByRole("menu", { name: "Session actions" });
  await expect(menu).toBeVisible();
  return menu;
}

test.describe("row ⋯ menu: session brief + hand off (#597 follow-up)", () => {
  test("Session brief opens the recap modal for the row", async ({ page }, testInfo) => {
    await setup(page, testInfo.project.name);
    const menu = await openRowMenu(page);
    await menu.getByRole("menuitem", { name: "Open session brief" }).click();

    const dialog = page.getByRole("dialog", { name: /fix the auth token/i });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("Root-caused intermittent 401s");
  });

  test("Hand off opens the capability-driven handoff modal for the row", async ({
    page,
  }, testInfo) => {
    await setup(page, testInfo.project.name);
    const menu = await openRowMenu(page);
    await menu.getByRole("menuitem", { name: "Hand off session to another engine" }).click();

    const dialog = page.getByRole("dialog", { name: /hand off/i });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(TITLE); // the FROM line names the source row
    // Capability-driven picker rendered (codex enabled; the non-agent shell never offered).
    await expect(dialog.getByRole("radio", { name: /codex/i })).toBeVisible();
    await expect(dialog.getByRole("radio", { name: /shell/i })).toHaveCount(0);
  });
});
