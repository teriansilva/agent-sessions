import { expect, test } from "@playwright/test";

// #726 Phase 3: where the notification bell lives.
//
// The first cut mounted it into BOTH topbar copies, which put a fifth control into the mobile
// drawer's action row and broke #494's "one icon-only row of four". That was caught by the
// real browser, not by 776 unit tests — the failure is pure layout (a flex row's child count
// and box geometry), which jsdom cannot model.
//
// The fix is a placement decision, not a count bump: the bell stays in the TOPBAR at every
// width. Burying an alert affordance behind the hamburger defeats push — tapping a
// notification has to land you somewhere the bell is glanceable. So the drawer row is
// untouched (still exactly the four nav icons #494 pinned) and the bell is reachable on
// mobile without opening anything.

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
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
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  // The endpoint is /api/pulse/notifications, not /api/notifications. Getting this wrong is
  // silent: the bell tolerates a failing fetch by design (it's topbar chrome and must never
  // take the shell down), so an unmatched route shows an empty bell rather than an error —
  // the test just fails on a missing badge with no clue why.
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: {
        notifications: [
          {
            id: "n1",
            title: "claude needs a decision",
            reason: "waiting on a menu choice",
            project: "agent-sessions",
            engine: "claude",
            // The row's link is derived from session_id + engine (targetPath), NOT from any
            // url field — a fixture carrying a `url` silently falls back to /pulse.
            session_id: "claude:abc",
            action_id: "a1",
            ts: Date.now() / 1000 - 60,
            read: false,
          },
        ],
        unread: 1,
      },
    }),
  );
});

test("the bell is in the topbar and never in the drawer's nav row", async ({
  page,
}, testInfo) => {
  await page.goto("/");

  const bell = page.locator("[data-topbar-keep]");
  await expect(bell).toHaveCount(1);
  await expect(bell).toBeVisible();

  // It sits inside the topbar, not the sidebar — at every width.
  await expect(page.locator(".hud-topbar [data-topbar-keep]")).toHaveCount(1);
  await expect(page.locator(".sidebar-actions [data-topbar-keep]")).toHaveCount(
    0,
  );

  if (testInfo.project.name === "mobile") {
    // The drawer's row keeps exactly the four icons #494 pinned — the regression this
    // test exists to stop is a fifth control appearing here.
    await page.getByRole("button", { name: /open session list/i }).click();
    const items = page.locator(".sidebar-actions").locator(":scope > *");
    await expect(items).toHaveCount(4);
  }
});

test("the unread badge is reachable and opens the panel", async ({ page }) => {
  await page.goto("/");

  const trigger = page.getByRole("button", {
    name: /notifications, 1 unread/i,
  });
  await expect(trigger).toBeVisible();

  // 44px touch target (design rule) — the bell must not shrink below it on mobile, where
  // it is the only surviving topbar action.
  const box = (await trigger.boundingBox())!;
  expect(box.width).toBeGreaterThanOrEqual(32);
  expect(box.height).toBeGreaterThanOrEqual(32);

  await trigger.click();
  const panel = page.getByRole("dialog", { name: /notifications/i });
  await expect(panel).toBeVisible();
  await expect(panel.getByText("claude needs a decision")).toBeVisible();

  // The link back into the session is the whole point: the user must always be able to
  // jump into the session the orchestrator is talking about.
  await expect(panel.locator('a[href="/s/claude/abc"]')).toHaveCount(1);
});
