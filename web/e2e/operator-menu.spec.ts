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

test("the corner names the operator, and keeps the bell and the gear beside it", async ({
  page,
  isMobile,
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

  // The gear is beside them on a desktop; on a phone the whole action cluster except the two
  // kept controls rides the drawer, and the TILE'S OWN MENU is what keeps Settings one tap from
  // the corner. Both are asserted, because "settings next to the avatar" has to remain true at
  // both widths — just not in the same place.
  const gear = actions.getByRole("link", { name: "Settings" });
  if (isMobile) {
    await expect(gear).toBeHidden();
  } else {
    await expect(gear).toBeVisible();
    const gbox = (await gear.boundingBox())!;
    expect(gbox.x).toBeGreaterThan(bb.x);
    expect(tb.x).toBeGreaterThan(gbox.x);
  }
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
