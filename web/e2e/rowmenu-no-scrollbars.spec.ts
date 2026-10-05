/** The row ⋯ menu's HUD corner brackets must not paint a scrollbar (#menu-scrollbars).
 *
 *  The popover is bounded by the viewport and scrolls only when its items genuinely
 *  exceed that bound. Its two decorative corner brackets used to sit 1px outside the
 *  box (`bottom: -1px; right: -1px`), which grew the *scrollable overflow* area by
 *  1px on each axis. A menu whose content fit therefore still showed a vertical AND
 *  a horizontal scrollbar — a dead control on a menu with nothing to scroll.
 *
 *  Real-browser proof on both projects: the desktop popover (the reported surface)
 *  must have no overflow on either axis, and the mobile sheet (which legitimately
 *  scrolls vertically on a short phone) must not grow a horizontal one.
 */
import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

const ENGINE = "claude";
const UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race";

// The row from the report's screenshot: every optional group is present, so the menu
// carries its full item set (brief, hand off, adopt, review ×3, row management).
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
  mission: null,
  review_excluded: false,
  orchestrator_excluded: false,
};

async function setup(page: Page): Promise<void> {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        ai_review: { configured: true },
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)

  await page.goto("/");
  // Desktop renders the sidebar as a grid column; mobile hides it behind the drawer toggle.
  if (page.viewportSize()!.width <= 800) {
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

test.describe("row ⋯ menu: no spurious scrollbars", () => {
  test("the desktop popover shows no scrollbar when its items fit", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the reported surface is the desktop popover");
    await setup(page);
    const menu = await openRowMenu(page);

    // The full item set the report's screenshot shows is present and fits…
    await expect(
      menu.getByRole("menuitem", { name: "Adopt session to a mission" }),
    ).toBeVisible();
    await expect(
      menu.getByRole("menuitem", { name: "Review session now" }),
    ).toBeVisible();

    // …so the popover must not scroll on either axis. Measured as scrollable
    // overflow (scroll - client), which the decorative corner brackets used to
    // inflate by 1px each even though nothing was actually clipped.
    const overflow = await menu.evaluate((el) => ({
      x: el.scrollWidth - el.clientWidth,
      y: el.scrollHeight - el.clientHeight,
    }));
    expect(overflow, "the popover has no scrollable overflow").toEqual({
      x: 0,
      y: 0,
    });
  });

  test("the mobile sheet shows no horizontal scrollbar", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "the sheet is mobile-specific");
    await setup(page);
    const menu = await openRowMenu(page);

    // The sheet may legitimately scroll vertically on a short phone, but its
    // right-edge corner bracket must not grow the horizontal scroll area.
    const dx = await menu.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(dx, "the sheet has no horizontal overflow").toBe(0);
  });
});
