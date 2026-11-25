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
import { SOURCE_URL } from "../src/lib/links";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { animationsSettled } from "./nightjarShell";

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
  await page.goto(NEW_SESSION_PATH);
  const tile = page.getByTestId("operator-menu");
  await expect(tile).toBeVisible();
  await expect(tile).toHaveAccessibleName("Operator nightowl");

  const actions = page.locator(".hud-topbar .hud-topbar-actions");
  const bell = actions.getByRole("button", { name: /^Notifications/ });
  await expect(bell).toBeVisible();
  // Order, left to right: the bell, then the tile, with the tile last against the edge.
  const bb = (await bell.boundingBox())!;
  const tb = (await tile.boundingBox())!;
  expect(tb.x).toBeGreaterThan(bb.x);

  // Settings and Help are IN the tile's menu since #1085 — the corner is Ask (#1294), the bell and
  // the tile, at every width, with no ⚙ link or ? button of their own. Plus, on a wide screen,
  // the ☎ that calls HQ (#1397), hidden at ≤880px (the dashboard card and HQ's header carry it there).
  await expect(actions.getByRole("link", { name: "Settings" })).toHaveCount(0);
  await expect(actions.getByRole("button", { name: "Help" })).toHaveCount(0);
  await expect(actions.locator(":scope > *")).toHaveCount(4);
  await expect(actions.locator(":scope > *:visible")).toHaveCount(
    (page.viewportSize()?.width ?? 1440) <= 880 ? 3 : 4,
  );
});

test("the panel opens ABOVE the terminal pane and its items are actually clickable (#752/#987)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto(NEW_SESSION_PATH);
  await page.getByTestId("operator-menu").click();
  const panel = page.getByTestId("operator-menu-panel");
  await expect(panel).toBeVisible();
  await expect(page.getByTestId("operator-who")).toContainText("nightowl");

  // THE ASSERTION THAT MATTERS: the topmost element at the item's own centre is the item (or
  // inside it), not the pane painting over it. `toBeVisible` passed against the bug.
  // Polled: on a phone the menu is a sheet that slides up, so a single read can land mid-slide
  // (a flake late in long local runs). The bug this guards (the pane painting over the menu)
  // keeps it false however long it is asked.
  const item = panel.getByRole("menuitem", { name: "Settings" });
  await expect
    .poll(async () => {
      const box = (await item.boundingBox())!;
      return page.evaluate(
        ([x, y]) => {
          const el = document.elementFromPoint(x as number, y as number);
          return !!el?.closest('[data-testid="operator-menu-panel"]');
        },
        [box.x + box.width / 2, box.y + box.height / 2],
      );
    })
    .toBe(true);

  // …and the click lands where it looks like it will.
  await item.click();
  await expect(page).toHaveURL(/\/settings/);
});

test("Report an issue opens GitHub's new-issue form in a new tab, prefilled (#1742)", async ({
  page,
  context,
}) => {
  // The popup is stubbed at the CONTEXT (popups are new pages in it), so the spec never needs the
  // network: what it proves is that the click lands on the item in the portalled panel and the
  // tab opens at the right address with the body filled in.
  await context.route("https://github.com/**", (r) =>
    r.fulfill({ contentType: "text/html", body: "<title>new issue</title>" }),
  );
  await mockShell(page);
  await page.goto(NEW_SESSION_PATH);
  await page.getByTestId("operator-menu").click();
  const panel = page.getByTestId("operator-menu-panel");
  const item = panel.getByRole("menuitem", { name: /^Report an issue/ });
  await item.scrollIntoViewIfNeeded();
  const [popup] = await Promise.all([page.waitForEvent("popup"), item.click()]);
  const url = new URL(popup.url());
  expect(`${url.origin}${url.pathname}`).toBe(`${SOURCE_URL}/issues/new`);
  const body = url.searchParams.get("body") ?? "";
  expect(body).toContain("### What happened");
  // The shell passes the running version in (App.tsx): a dropped prop reads "unknown".
  expect(body).toMatch(/- BattleLab: \S+/);
  expect(body).not.toContain("- BattleLab: unknown");
  expect(body).toContain(`- Browser: ${await page.evaluate(() => navigator.userAgent)}`);
  // noopener: the opened tab has no handle back on BattleLab.
  expect(await popup.evaluate(() => window.opener)).toBeNull();
  await expect(panel).toHaveCount(0);
});

test("Escape closes it and returns focus to the tile", async ({ page }) => {
  await mockShell(page);
  await page.goto(NEW_SESSION_PATH);
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
  await page.goto(NEW_SESSION_PATH);
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
  await page.goto(NEW_SESSION_PATH);
  await page.getByTestId("operator-menu").click();
  const panel = page.getByTestId("operator-menu-panel");
  await expect(panel).toBeVisible();
  // A sheet/dialog slides in (#1531): measure where it lands.
  await animationsSettled(page);
  const box = (await panel.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(320);
  expect(await panel.evaluate((el) => getComputedStyle(el).overflowY)).toBe("auto");
  const signOut = panel.getByRole("menuitem", { name: /sign out/i });
  await signOut.scrollIntoViewIfNeeded();
  const s = (await signOut.boundingBox())!;
  expect(s.y + s.height).toBeLessThanOrEqual(320);
  // Actually hittable where it is drawn — not covered by anything.
  await signOut.click({ trial: true });
  // Report an issue (#1742) made the panel one row taller: it must be reachable here too.
  const report = panel.getByRole("menuitem", { name: /^Report an issue/ });
  await report.scrollIntoViewIfNeeded();
  const r = (await report.boundingBox())!;
  expect(r.y).toBeGreaterThanOrEqual(box.y);
  expect(r.y + r.height).toBeLessThanOrEqual(320);
  await report.click({ trial: true });
  // …and the keyboard reaches it too: End moves focus there, which scrolls it into view.
  await panel.getByRole("menuitem").first().focus();
  await page.keyboard.press("End");
  await expect(signOut).toBeFocused();
});

/** Reads one CSS pixel of a page screenshot, decoded by the browser itself (no PNG dependency). */
async function pixelAt(page: Page, x: number, y: number): Promise<number[]> {
  const shot = await page.screenshot({
    clip: { x: Math.floor(x), y: Math.floor(y), width: 1, height: 1 },
    scale: "css",
    animations: "disabled",
  });
  return page.evaluate(async (b64) => {
    const img = await createImageBitmap(await (await fetch(`data:image/png;base64,${b64}`)).blob());
    const ctx = new OffscreenCanvas(1, 1).getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    return Array.from(ctx.getImageData(0, 0, 1, 1).data.slice(0, 3));
  }, shot.toString("base64"));
}

test("the initials square is cut by the tile's chamfer — no corner pokes out of the plate", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto(NEW_SESSION_PATH);
  const tile = page.getByTestId("operator-menu");
  await expect(tile).toBeVisible();
  await animationsSettled(page);
  const tb = (await tile.boundingBox())!;

  // A point inside the plate's top-left cut (x + y well under --cut-sm) is the bar behind the tile,
  // so it paints like the bar just above the tile — not the square's accent tint, which it did
  // when the square was an unclipped rectangle sitting in that corner.
  const corner = await pixelAt(page, tb.x + 2, tb.y + 3);
  const bar = await pixelAt(page, tb.x + 2, tb.y - 2);
  for (let i = 0; i < 3; i++) expect(Math.abs(corner[i] - bar[i])).toBeLessThanOrEqual(10);
});
