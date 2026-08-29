import { expect, test, type Page } from "@playwright/test";

/** A session opened by LINK still names itself (#867) — real-browser proof, mobile + desktop.
 *
 * The regression this guards is a data-ownership one, not a layout one, but it is invisible to a
 * unit test of either consumer: the pane header and the Files trigger degrade only when the
 * SIDEBAR's list happens not to contain the open session, and that coupling lives across three
 * components and a route. So the mock below is the whole point — `/api/sessions` (the list) is
 * deliberately served WITHOUT the session being opened, exactly as the real server does for
 * anything past page 0, archived, or hidden by `projects_mode` / `projects_hidden`.
 *
 * Against `main` both assertions fail: the header renders a bare LED + engine box, and Files sits
 * disabled behind "This session has not reported a folder yet".
 */

const NOW = Math.floor(Date.now() / 1000);
const UUID = "aaaaaaaa-0000-4000-8000-0000000000ff";
const CWD = "/home/u/deep-linked-project";

/** The row the LIST never returns — only the single-session lookup does. */
const HIDDEN = {
  id: `claude:${UUID}`,
  engine: "claude",
  uuid: UUID,
  short_uuid: UUID.slice(0, 8),
  cwd: CWD,
  project: { kind: "folder", id: CWD, name: CWD },
  last_mtime: NOW - 172_800,
  created_at: NOW - 200_000,
  title: "deep-linked session",
  first_user_message: "hello",
  sticky: false,
  archived: false,
};

/** Someone else entirely — so "the list returned a page" is true, just not this session's.
 *  A distinct age, because it renders its own relative time in the sidebar and the header's
 *  must stay unambiguous. */
const OTHER = {
  ...HIDDEN,
  id: "claude:bbbb",
  uuid: "bbbb",
  cwd: "/home/u/other",
  title: "other",
  last_mtime: NOW - 3600,
};

async function mockApp(page: Page, opts: { lookup: boolean }) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        hostname: "test",
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/files/capabilities", (r) => r.fulfill({ json: { ok: true, reason: "" } }));

  // The LIST — one page that does NOT contain the session being opened, which is what the real
  // server returns for anything past page 0, archived, or hidden by the visibility scope.
  await page.route(/\/api\/sessions(\?|$)/, (r) =>
    r.fulfill({
      json: {
        sessions: [OTHER],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  // The single-session lookup. Registered AFTER the list because Playwright matches
  // most-recently-registered first, and anchored so the two can never both claim a URL — a glob
  // like `**/api/sessions?**` matches `/api/sessions/<id>` too, and silently served this pane a
  // page instead of a row. `lookup: false` reproduces pre-#867: the list and nothing else.
  await page.route(/\/api\/sessions\/[^/?]+$/, (r) =>
    opts.lookup ? r.fulfill({ json: HIDDEN }) : r.fulfill({ status: 404, json: { detail: "x" } }),
  );
}

async function open(page: Page, lookup: boolean) {
  await mockApp(page, { lookup });
  await page.goto(`/s/claude/${UUID}`);
  await expect(page.locator("#root")).toBeVisible();
  await page.locator("[data-head-action]").first().waitFor();
}

test("a deep-linked session names its project in the pane header", async ({ page }, info) => {
  await open(page, true);
  // The #744 meta run, populated from the pane's own lookup rather than the sidebar's page.
  await expect(page.getByText("~/deep-linked-project")).toBeVisible();
  // And the full launch folder is answerable without opening the file panel.
  await expect(page.getByTitle(CWD)).toBeVisible();
  // The update time is the FIRST thing #744's collapse ladder drops (`@container panelhead
  // (max-width: 520px)`), and a phone pane is narrower than that — so asserting it on mobile
  // would be asserting against the header's own design, not against this fix. The project
  // survives to 360px, which is why it is checked on both.
  if (info.project.name === "desktop") {
    await expect(page.getByText(/2 days ago/)).toBeVisible();
  }
});

test("a deep-linked session can open its files — the trigger is not disabled", async ({ page }) => {
  await open(page, true);
  const direct = page.locator("[data-head-action='files']");
  const trigger = (await direct.count())
    ? direct
    : (await page.getByRole("button", { name: "More session actions" }).click(),
      page.getByRole("menuitem", { name: /Files/ }));
  await expect(trigger).toBeEnabled();
});

test("without the lookup the pane is nameless — the bug this fixes", async ({ page }) => {
  // The control: same list, lookup unavailable. Pins that the assertions above are actually
  // measuring the lookup and not something the mock provides for free.
  await open(page, false);
  await expect(page.getByText("~/deep-linked-project")).toHaveCount(0);
  await expect(page.getByText(/2 days ago/)).toHaveCount(0);
});
