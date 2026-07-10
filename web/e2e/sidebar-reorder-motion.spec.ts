import { expect, test, type Page } from "@playwright/test";

function sess(id: string, title: string, mtime: number) {
  return {
    id: `claude:${id}`,
    engine: "claude",
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: "/home/u/x",
    project: { kind: "folder", id: "/home/u/x", name: "/home/u/x" },
    last_mtime: mtime,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
  };
}

async function setup(page: Page, project: string): Promise<{ promote: () => void }> {
  const now = Math.floor(Date.now() / 1000);
  const alpha = sess("aaaaaaaa-0000-0000-0000-000000000001", "Alpha session", now - 10);
  const bravo = sess("bbbbbbbb-0000-0000-0000-000000000002", "Bravo session", now - 120);
  let promoted = false;

  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        session_list_order: "recent_activity",
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: promoted
          ? [{ ...bravo, last_mtime: now + 10 }, alpha]
          : [alpha, bravo],
        next_offset: null,
        total: 2,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));

  await page.goto("/");
  if (project === "mobile") {
    await page.getByRole("button", { name: "Open session list" }).click();
  }
  await expect(page.getByRole("link", { name: /Alpha session/ })).toBeVisible();
  await expect(page.locator('aside a[href^="/s/"]').first()).toContainText("Alpha session");

  return {
    promote: () => {
      promoted = true;
    },
  };
}

test("rows animate when a refresh re-sorts by recent activity (#607)", async ({
  page,
}, testInfo) => {
  const state = await setup(page, testInfo.project.name);
  state.promote();

  // Force the hook's visibility catch-up refresh without waiting for the 15s poll interval.
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });

  const movingRows = page.locator('li[data-reorder-motion="true"]');
  await expect(movingRows.first()).toBeVisible();
  await expect(page.locator('aside a[href^="/s/"]').first()).toContainText("Bravo session");
});
