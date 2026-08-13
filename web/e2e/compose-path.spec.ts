import { type Page, expect, test } from "@playwright/test";

/** Sending a path from the file panel into the compose draft (#792) — desktop AND mobile.
 *
 *  The assertions that jsdom cannot make: that the send control and the open-the-viewer control
 *  are two real, separately-hittable targets in the same row rather than one nested inside the
 *  other; that the mobile sheet gets out of the way so the result is visible; and that the whole
 *  SessionView → FilePanel → row → Terminal → Compose chain is actually wired, which is the part
 *  a component test would stub straight past.
 */

const NOW = Math.floor(Date.now() / 1000);
const CWD = "/home/u/proj";

const SESSION = {
  id: "claude:aaaaaaaa-0000-4000-8000-000000000001",
  engine: "claude",
  title: "compose path session",
  cwd: CWD,
  project: { kind: "folder", id: CWD, label: "proj" },
  last_mtime: NOW - 120,
  archived: false,
  favorite: false,
};

/** `hostile.txt` is not decoration: a real repo can hold a name like this, and the token that
 *  reaches the draft has to be the quoted form, not the raw name. */
const ENTRIES = [
  { name: "src", path: `${CWD}/src`, kind: "dir", size: 4096, mtime: NOW },
  {
    name: "README.md",
    path: `${CWD}/README.md`,
    kind: "file",
    size: 512,
    mtime: NOW,
  },
  {
    name: "notes.txt",
    path: `${CWD}/notes.txt`,
    kind: "file",
    size: 12,
    mtime: NOW,
  },
  {
    name: "; rm -rf ~",
    path: `${CWD}/; rm -rf ~`,
    kind: "file",
    size: 3,
    mtime: NOW,
  },
];

async function mockApp(page: Page) {
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [SESSION],
        next_offset: null,
        total: 1,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/files/capabilities", (r) =>
    r.fulfill({ json: { ok: true, reason: "" } }),
  );
  await page.route("**/api/files/list**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        parent: "/home/u",
        root: "/home/u",
        entries: ENTRIES,
        total: ENTRIES.length,
        complete: true,
        truncated: false,
      },
    }),
  );
  await page.route("**/api/files/read**", (r) =>
    r.fulfill({
      json: {
        path: CWD,
        size: 10,
        binary: false,
        content: "content\n",
        truncated: false,
      },
    }),
  );
  await page.route("**/api/git/status**", (r) =>
    r.fulfill({ json: { repo: null } }),
  );
  // The draft PUT must not 404 into a console error while we assert on the textarea.
  await page.route("**/api/sessions/*/draft", (r) => r.fulfill({ json: {} }));
}

/** Open the panel WITHOUT navigating. Re-running `openPanel` mid-test would reload the page and
 *  throw away the draft under test — which is exactly the state these tests are about. */
async function showPanel(page: Page) {
  const direct = page.locator("[data-head-action='files']");
  if (await direct.count()) await direct.click();
  else {
    await page.getByRole("button", { name: "More session actions" }).click();
    await page.getByRole("menuitem", { name: /Files/ }).click();
  }
  await expect(page.locator("[data-file-panel]")).toBeVisible();
}

async function openPanel(page: Page) {
  await mockApp(page);
  await page.goto(`/s/claude/${SESSION.id.split(":")[1]}`);
  await page.locator("[data-head-action]").first().waitFor();
  await showPanel(page);
}

// By placeholder, not by tag: the page carries more than one textarea, and a bare `textarea`
// locator is a strict-mode violation rather than a helpful failure.
const draft = (page: Page) => page.getByPlaceholder(/Type here/);

test("a row's send control puts the path in the draft without sending it", async ({
  page,
}) => {
  await openPanel(page);
  await page.locator(`[data-send-path$="README.md"]`).click();

  // Inserted, never sent: the text is sitting in the box for the user to write around.
  await expect(draft(page)).toHaveValue("README.md");
  // And the send did NOT also trigger the row behind it — the other half of "two targets".
  await expect(page.locator("[data-file-viewer]")).toHaveCount(0);
});

test("two sends accumulate with exactly one separator", async ({ page }) => {
  await openPanel(page);
  await page.locator(`[data-send-path$="README.md"]`).click();
  // On mobile the first send closes the sheet, so re-show it — without navigating, or the draft
  // this test is measuring would be reloaded away.
  if (!(await page.locator("[data-file-panel]").isVisible())) await showPanel(page);
  await page.locator(`[data-send-path$="notes.txt"]`).click();

  const value = await draft(page).inputValue();
  expect(value).toContain("README.md");
  expect(value).toContain("notes.txt");
  expect(value).not.toContain("  ");
});

test("a hostile filename arrives quoted, not raw", async ({ page }) => {
  await openPanel(page);
  await page.locator(`[data-send-path$="rm -rf ~"]`).click();

  // Bare, this is three shell words and one of them deletes a home directory. The draft must
  // carry the quoted form — this is the whole reason pathToken exists.
  await expect(draft(page)).toHaveValue(`'; rm -rf ~'`);
});

test("the row and its send control are two separate targets", async ({
  page,
}) => {
  await openPanel(page);
  const row = page.locator(`[data-file-row]`, { hasText: "README.md" });

  // Hitting the ROW still opens the viewer — the new control did not swallow the row's job.
  await row.click();
  await expect(page.locator("[data-file-viewer]")).toBeVisible();
  // ...and it did not also send. On desktop Compose is collapsed until something opens it, so
  // "no draft box at all" is the honest form of "nothing was inserted".
  const box = draft(page);
  if (await box.count()) await expect(box).toHaveValue("");
});

test("a directory has no send control", async ({ page }) => {
  await openPanel(page);
  await expect(page.locator(`[data-send-path$="/src"]`)).toHaveCount(0);
  await expect(page.locator(`[data-send-path$="README.md"]`)).toHaveCount(1);
});

test.describe("touch", () => {
  test.skip(({ isMobile }) => !isMobile, "coarse-pointer behaviour only");

  test("the control is visible without hover and is a 44px target", async ({
    page,
  }) => {
    await openPanel(page);
    const send = page.locator(`[data-send-path$="README.md"]`);
    // A coarse pointer never hovers, so an opacity-on-hover control would exist and never be
    // reachable. It must be visible on its own.
    await expect(send).toBeVisible();
    const box = await send.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
  });

  test("the sheet closes so the draft is actually visible", async ({
    page,
  }) => {
    await openPanel(page);
    await page.locator(`[data-send-path$="README.md"]`).tap();

    // A confirmation behind an opaque sheet is not a confirmation.
    await expect(page.locator("[data-file-panel]")).toBeHidden();
    await expect(draft(page)).toHaveValue("README.md");
  });
});

test.describe("desktop", () => {
  test.skip(({ isMobile }) => isMobile, "dock behaviour only");

  test("the dock stays open, because it never covered the draft", async ({
    page,
  }) => {
    await openPanel(page);
    await page.locator(`[data-send-path$="README.md"]`).click();

    await expect(page.locator("[data-file-panel]")).toBeVisible();
    await expect(draft(page)).toHaveValue("README.md");
  });
});
