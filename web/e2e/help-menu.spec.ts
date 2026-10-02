import { expect, test, type Page } from "@playwright/test";

import { DOCS_HOME_URL } from "../src/lib/links";
import { mockRoster } from "./roster";

/** Help — in the operator menu since #1085, the `?` menu before (#987) — and the setup wizard's docs
 *  links, in a real browser (desktop + mobile).
 *
 * The server is mocked. `whats_new_seen` already covers 0.20 and the build is unstamped, so What's
 * new never opens on its own here — every dialog in these tests was opened by the test. The docs
 * host is routed to a stub page, so following the Documentation link never leaves the test. */

const NOOP_WS = `
window.WebSocket = class {
  constructor() { this.readyState = 0; this.binaryType = "arraybuffer";
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 20); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
};
`;

async function setup(page: Page, { onboarded = true }: { onboarded?: boolean } = {}) {
  await page.addInitScript(NOOP_WS);
  // Catch-all first: Playwright consults the most recently registered route first.
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/projects**", (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [{ id: "claude", present: true, supports_new: true, bin: "/x/claude" }] } }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [{ cwd: "/home/op/projects/upload-svc", label: "upload-svc" }] } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "0.20.0" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        terminal_backend: "ws",
        new_session_engines: ["claude"],
        onboarded,
        whats_new_seen: "0.20.0",
      },
    }),
  );
  await page.context().route(`${DOCS_HOME_URL}**`, (r) =>
    r.fulfill({ contentType: "text/html", body: "<title>BattleLab Docs</title><h1>Docs</h1>" }),
  );
}

/** Help lives in the operator menu since #1085 — the tile in the corner, which stays in the top
 *  bar at every width, so the same surface serves desktop and phone. `auth_mode: "none"` names it
 *  "Operator — no login". */
const MENU_NAME = "Operator — no login";
const trigger = (page: Page) => page.getByTestId("operator-menu");
const helpMenu = (page: Page) => page.getByRole("menu", { name: MENU_NAME });

test("the operator menu carries Settings and Help: first item focused, arrows move, Escape closes and returns focus", async ({
  page,
}) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  const t = trigger(page);
  await expect(t).toHaveAttribute("aria-haspopup", "menu");
  // No `?` and no ⚙ of their own any more (#1085).
  await expect(page.locator(".hud-topbar").getByRole("button", { name: "Help" })).toHaveCount(0);
  await expect(page.locator(".hud-topbar").getByRole("link", { name: "Settings" })).toHaveCount(0);
  await t.click();

  const menu = helpMenu(page);
  await expect(menu).toBeVisible();
  await expect(t).toHaveAttribute("aria-expanded", "true");
  await expect(menu.getByRole("menuitem")).toHaveText([
    "Settings",
    "Security & 2FA",
    "Intro tour",
    `Documentation${new URL(DOCS_HOME_URL).host}`,
    "What's new in 0.20",
  ]);
  await expect(menu.getByRole("menuitem", { name: "Settings" })).toBeFocused();

  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem", { name: "Security & 2FA" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(menu.getByRole("menuitem", { name: "What's new in 0.20" })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(menu.getByRole("menuitem", { name: "Settings" })).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(menu.getByRole("menuitem", { name: "What's new in 0.20" })).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(t).toHaveAttribute("aria-expanded", "false");
  await expect(t).toBeFocused();
});

test("a second click closes it, a double-click does not leave it open, and an outside press closes it", async ({
  page,
}) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  const t = trigger(page);

  await t.click();
  await expect(helpMenu(page)).toBeVisible();
  await t.click();
  await expect(helpMenu(page)).toHaveCount(0);

  await t.dblclick();
  await page.waitForTimeout(150);
  await expect(helpMenu(page)).toHaveCount(0);

  await t.click();
  await expect(helpMenu(page)).toBeVisible();
  const vp = page.viewportSize()!;
  await page.mouse.click(40, vp.height - 60);
  await expect(helpMenu(page)).toHaveCount(0);
});

test("Tab and Shift+Tab leave the menu: it closes and focus is back on the tile", async ({ page }) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  const t = trigger(page);

  for (const key of ["Tab", "Shift+Tab"]) {
    await t.click();
    await expect(helpMenu(page)).toBeVisible();
    await page.keyboard.press(key);
    await expect(helpMenu(page), key).toHaveCount(0);
    await expect(t, key).toBeFocused();
  }
});

test("Intro tour opens the tour, What's new opens the dialog, Settings opens Settings", async ({
  page,
}) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");

  await trigger(page).click();
  await helpMenu(page).getByRole("menuitem", { name: "Intro tour" }).click();
  const tour = page.getByRole("dialog", { name: "Tour" });
  await expect(tour).toBeVisible();
  await expect(helpMenu(page)).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(tour).toHaveCount(0);

  await trigger(page).click();
  await helpMenu(page).getByRole("menuitem", { name: "What's new in 0.20" }).click();
  const dialog = page.getByRole("dialog", { name: /what's new/i });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  await trigger(page).click();
  await helpMenu(page).getByRole("menuitem", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/settings/);
});

test("Documentation opens the docs home in a new tab with no opener", async ({ page }) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  await trigger(page).click();
  const docs = helpMenu(page).getByRole("menuitem", { name: "Documentation (opens in a new tab)" });
  await expect(docs).toHaveAttribute("href", DOCS_HOME_URL);
  await expect(docs).toHaveAttribute("target", "_blank");
  await expect(docs).toHaveAttribute("rel", "noopener noreferrer");

  const [popup] = await Promise.all([page.waitForEvent("popup"), docs.click()]);
  await popup.waitForLoadState();
  expect(popup.url()).toBe(DOCS_HOME_URL);
  expect(await popup.evaluate(() => window.opener)).toBeNull();
  await popup.close();
  await expect(helpMenu(page)).toHaveCount(0);
});

test("measured: 44px rows, and the panel sits inside the viewport under the tile", async ({
  page,
}) => {
  await setup(page);
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  const t = trigger(page);
  await t.click();
  const menu = helpMenu(page);
  await expect(menu).toBeVisible();
  for (const item of await menu.getByRole("menuitem").all()) {
    expect((await item.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  const vw = page.viewportSize()!.width;
  const m = (await menu.boundingBox())!;
  const tb = (await t.boundingBox())!;
  expect(m.x).toBeGreaterThanOrEqual(0);
  expect(m.x + m.width).toBeLessThanOrEqual(vw);
  expect(m.y).toBeGreaterThanOrEqual(tb.y + tb.height);
});

test("the setup wizard's Welcome and Launch steps link the docs in a new tab and stay on their step", async ({
  page,
}) => {
  await setup(page, { onboarded: false });
  await mockRoster(page, { only: ["claude"] }); // the manifest-generated roster (#853 P4)
  await page.goto("/");
  const wizard = page.getByRole("dialog", { name: "Set up BattleLab" });
  await expect(wizard.getByRole("heading", { name: "Welcome to BattleLab" })).toBeVisible();

  const docsButton = wizard.getByRole("link", { name: "Docs (opens in a new tab)" });
  await expect(docsButton).toHaveAttribute("href", DOCS_HOME_URL);
  await expect(docsButton).toHaveAttribute("target", "_blank");
  await expect(docsButton).toHaveAttribute("rel", "noopener noreferrer");
  const [popup] = await Promise.all([page.waitForEvent("popup"), docsButton.click()]);
  await popup.waitForLoadState();
  expect(popup.url()).toBe(DOCS_HOME_URL);
  await popup.close();
  await expect(wizard.getByRole("heading", { name: "Welcome to BattleLab" })).toBeVisible();

  // Walk to the Launch step: every step's forward control, the tour's slides, then Finish tour.
  await wizard.getByRole("button", { name: /Get started/ }).click();
  await wizard.getByRole("button", { name: /Skip — continue/ }).click();
  await wizard.getByRole("button", { name: /^Next/ }).click();
  await wizard.getByRole("button", { name: /I'll do this later/ }).click();
  await wizard.getByRole("button", { name: /^Next/ }).click();
  await expect(wizard.getByRole("heading", { name: "The lay of the land" })).toBeVisible();
  const finish = wizard.getByRole("button", { name: /Finish tour/ });
  while (!(await finish.isVisible())) await wizard.getByRole("button", { name: /^Next/ }).click();
  await finish.click();
  await expect(wizard.getByRole("heading", { name: "Start your first session" })).toBeVisible();

  const docsLink = wizard.getByRole("link", { name: "docs", exact: true });
  await expect(docsLink).toHaveAttribute("href", DOCS_HOME_URL);
  await expect(docsLink).toHaveAttribute("target", "_blank");
  await expect(docsLink).toHaveAttribute("rel", "noopener noreferrer");
  const [popup2] = await Promise.all([page.waitForEvent("popup"), docsLink.click()]);
  await popup2.waitForLoadState();
  expect(popup2.url()).toBe(DOCS_HOME_URL);
  await popup2.close();
  await expect(wizard.getByRole("heading", { name: "Start your first session" })).toBeVisible();
});
