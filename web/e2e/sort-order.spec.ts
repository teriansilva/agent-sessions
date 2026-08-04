import { expect, test, type Page } from "@playwright/test";

// #506: real-browser proof for the session-list sort-order toggle in Settings → Appearance.
// The radio click + persisted POST is exercised here; the actual list re-sort is server-side
// (covered exhaustively by tests/test_sort_order.py — both modes, sticky precedence, per-engine
// created_at, the 422 guard). Run once (desktop project) — the control is identical on mobile.

async function setup(page: Page): Promise<unknown[]> {
  const posts: unknown[] = [];
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
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route("**/api/prefs", (r) => {
    posts.push(r.request().postDataJSON());
    return r.fulfill({ json: { session_list_order: "created_at" } });
  });
  await page.goto("/settings/appearance");
  return posts;
}

test.describe("session list sort order (#506)", () => {
  test("toggling to Creation date persists session_list_order", async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "the control is identical on mobile; run once",
    );
    const posts = await setup(page);

    const recent = page.getByRole("radio", { name: /Recent activity/ });
    const created = page.getByRole("radio", { name: /Creation date/ });
    await expect(recent).toBeVisible();
    // Defaults to recent activity.
    await expect(recent).toHaveAttribute("aria-checked", "true");
    await expect(created).toHaveAttribute("aria-checked", "false");

    await created.click();
    // Optimistic: the chosen card flips immediately…
    await expect(created).toHaveAttribute("aria-checked", "true");
    await expect(recent).toHaveAttribute("aria-checked", "false");
    // …and the choice is persisted via /api/prefs.
    await expect
      .poll(() => posts)
      .toContainEqual({ session_list_order: "created_at" });
  });
});

// #548: the toggle also lives in the sidebar header (replacing the "Sessions / SEC // 01"
// label row) and re-sorts the list IN PLACE — the pref save refreshes the shared config,
// whose order change triggers a page-0 refetch; the server returns the re-sorted rows.
// The mock is stateful to model that: /api/sessions answers by the last-persisted order.
function sess(id: string, title: string, mtime: number, created: number) {
  return {
    id: `claude:${id}`,
    engine: "claude",
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: "/home/u/x",
    project: { kind: "folder", id: "/home/u/x", name: "/home/u/x" },
    last_mtime: mtime,
    created_at: created,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
  };
}

async function setupSidebar(page: Page): Promise<void> {
  const NOW = Math.floor(Date.now() / 1000);
  // "updated-latest" has the newest activity; "created-latest" was created most recently.
  const byActivity = [
    sess(
      "aaaaaaaa-0000-0000-0000-000000000001",
      "updated-latest",
      NOW,
      NOW - 9000,
    ),
    sess(
      "bbbbbbbb-0000-0000-0000-000000000002",
      "created-latest",
      NOW - 5000,
      NOW - 100,
    ),
  ];
  const byCreation = [byActivity[1], byActivity[0]];
  let order = "recent_activity";
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        session_list_order: order,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: order === "created_at" ? byCreation : byActivity,
        next_offset: null,
        total: 2,
        facets: { projects: [], engines: ["claude"] },
      },
    }),
  );
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route(/\/api\/projects($|\?)/, (r) =>
    r.fulfill({ json: { projects: [] } }),
  );
  await page.route("**/api/prefs", (r) => {
    const body = r.request().postDataJSON() as { session_list_order?: string };
    if (body.session_list_order) order = body.session_list_order;
    return r.fulfill({ json: { session_list_order: order } });
  });
  await page.goto("/");
}

test.describe("sidebar sort-order toggle (#548)", () => {
  for (const project of ["desktop", "mobile"] as const) {
    test(`${project}: header toggle replaces SEC // 01 and re-sorts the list in place`, async ({
      page,
    }, testInfo) => {
      test.skip(testInfo.project.name !== project, `${project}-only variant`);
      await setupSidebar(page);
      if (project === "mobile") {
        // The sidebar is an off-canvas drawer on mobile — open it first.
        await page.getByRole("button", { name: "Open session list" }).click();
      }

      // The decorative header content is gone; the toggle owns the row.
      await expect(page.getByText("SEC // 01")).toBeHidden();
      const group = page.getByRole("radiogroup", { name: "Order" });
      await expect(
        group.getByRole("radio", { name: "Recent" }),
      ).toHaveAttribute("aria-checked", "true");

      // Recent-activity order: the newest-updated session leads.
      const firstRow = page.locator('aside a[href^="/s/"]').first();
      await expect(firstRow).toContainText("updated-latest");

      await group.getByRole("radio", { name: "Created" }).click();
      await expect(
        group.getByRole("radio", { name: "Created" }),
      ).toHaveAttribute("aria-checked", "true");
      // The refetch lands the creation order without navigating anywhere.
      await expect(firstRow).toContainText("created-latest");
    });
  }
});
