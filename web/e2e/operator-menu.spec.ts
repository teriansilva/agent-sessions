/** The operator tile in the corner (#1058), in a real browser — because the bug class it belongs to
 *  only exists in one.
 *
 *  `.hud-topbar` has `backdrop-filter` and no `z-index`, which makes it a stacking context the
 *  terminal pane paints over. A panel anchored inside it DRAWS, takes focus, and hands the click to
 *  the pane underneath: the notification bell's #752 and the Help menu's #987, twice. jsdom has no
 *  stacking contexts and no hit testing, so a unit test cannot see it at all, and asserting the
 *  panel is "visible" cannot either — visible is exactly what it was. So these specs CLICK an item
 *  and check the click landed.
 */
import { expect, test, type Page } from "@playwright/test";

async function mockShell(
  page: Page,
  cfg: { username?: string | null; auth_mode?: string } = {},
) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: cfg.auth_mode ?? "single-user",
        username: cfg.username === undefined ? "nightowl" : cfg.username,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
}

test("the corner names the operator beside the bell, and no gear or ? of its own (#1085)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/");
  const tile = page.getByTestId("operator-menu");
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAccessibleName("Operator nightowl");

  const actions = page.locator(".hud-topbar .hud-topbar-actions");
  const bell = actions.locator("[data-topbar-keep] button").first();
  await expect(bell).toBeVisible();
  // Order, left to right: the bell, then the tile, with the tile last against the edge.
  const bb = (await bell.boundingBox())!;
  const tb = (await tile.boundingBox())!;
  expect(tb.x).toBeGreaterThan(bb.x);

  // Settings and Help are IN the tile's menu since #1085 — the corner is the bell and the tile,
  // at every width, with no ⚙ link or ? button of their own.
  await expect(actions.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await expect(actions.getByRole("button", { name: "Help" })).toHaveCount(0);
  await expect(actions.locator(":scope > *")).toHaveCount(2);
});

test("the panel opens ABOVE the terminal pane and its items are actually clickable (#752/#987)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/");
  await page.getByTestId("operator-menu").click();
  const panel = page.getByTestId("operator-menu-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("operator-who")).toContainText("nightowl");

  // THE ASSERTION THAT MATTERS: the topmost element at the item's own centre is the item (or
  // inside it), not the pane painting over it. `toBeVisible` passed against the bug.
  const item = panel.getByRole("menuitem", { name: "Settings" });
  const box = (await item.boundingBox())!;
  const owns = await page.evaluate(
    ([x, y]) => {
      const el = document.elementFromPoint(x as number, y as number);
      return !!el?.closest('[data-testid="operator-menu-panel"]');
    },
    [box.x + box.width / 2, box.y + box.height / 2],
  );
  expect(owns).toBe(true);

  // …and the click lands where it looks like it will.
  await item.click();
  await expect(page).toHaveURL(/\/settings/);
});

test("Escape closes it and returns focus to the tile", async ({ page }) => {
  await mockShell(page);
  await page.goto("/");
  const tile = page.getByTestId("operator-menu");
  await tile.click();
  await expect(page.getByTestId("operator-menu-panel")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("operator-menu-panel")).toHaveCount(0);
  await expect(tile).toBeFocused();
});

test("a no-login install reads LOCAL and offers no Sign out", async ({
  page,
}) => {
  // `auth_mode: "none"` has no session to end. A Sign out here would be a control the app cannot
  // honour, and a username would claim a login the install does not have.
  await mockShell(page, { auth_mode: "none", username: null });
  await page.goto("/");
  const tile = page.getByTestId("operator-menu");
  await expect(tile).toHaveAccessibleName(/no login/i);
  await tile.click();
  await expect(page.getByTestId("operator-who")).toContainText(/no login/i);
  await expect(page.getByTestId("operator-sign-out")).toHaveCount(0);
  await expect(
    page.getByTestId("operator-menu-panel").getByRole("menuitem", {
      name: "Settings",
    }),
  ).toBeVisible();
});

test("on a short landscape screen every item — Sign out included — stays reachable (#1089)", async ({
  page,
}) => {
  // Hermes on #1089: seven rows at 667×320 put Sign out below the viewport, and the page itself
  // cannot scroll. The panel is bounded to the room under the tile and scrolls inside itself.
  await page.setViewportSize({ width: 667, height: 320 });
  await mockShell(page);
  await page.goto("/");
  await page.getByTestId("operator-menu").click();
  const panel = page.getByTestId("operator-menu-panel");
  await expect(panel).toBeVisible();
  const box = (await panel.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(320);
  expect(await panel.evaluate((el) => getComputedStyle(el).overflowY)).toBe("auto");
  const signOut = panel.getByRole("menuitem", { name: /sign out/i });
  await signOut.scrollIntoViewIfNeeded();
  const s = (await signOut.boundingBox())!;
  expect(s.y + s.height).toBeLessThanOrEqual(320);
  // Actually hittable where it is drawn — not covered by anything.
  await signOut.click({ trial: true });
  // …and the keyboard reaches it too: End moves focus there, which scrolls it into view.
  await panel.getByRole("menuitem").first().focus();
  await page.keyboard.press("End");
  await expect(signOut).toBeFocused();
});
