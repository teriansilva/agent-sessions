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
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/prefs", (r) => {
    posts.push(r.request().postDataJSON());
    return r.fulfill({ json: { session_list_order: "created_at" } });
  });
  await page.goto("/settings/appearance");
  return posts;
}

test.describe("session list sort order (#506)", () => {
  test("toggling to Creation date persists session_list_order", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "the control is identical on mobile; run once");
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
    await expect.poll(() => posts).toContainEqual({ session_list_order: "created_at" });
  });
});
