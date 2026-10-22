/** HQ contacts (#1525), in a real browser on desktop and mobile: the dashboard as the start page,
 *  the primary contact's card (or Off's placeholder), the HQ header, the Settings gallery, the daily
 *  Random pick, the media rules (reduced motion, paused, failed) and answers labelled by speaker. */
import { expect, test, type Page } from "@playwright/test";

import { pickConversation } from "./askSidebar";

import { CONTACTS, contactForDay } from "../src/components/hq/contacts";
import { DASHBOARD_PATH, NEW_SESSION_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";
import { ASK_STREAM, fulfillAsk, mockAskConversations } from "./askStream";

const name = (id: string) => CONTACTS.find((c) => c.id === id)!.name;

/** The shell, with a STATEFUL `hq_contact`: a prefs write changes what the next config read says. */
async function mockShell(page: Page, hqContact: string) {
  const state = { hq_contact: hqContact, rev: 0, writes: [] as unknown[] };
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
        hq_contact: state.hq_contact,
        hq_contact_rev: state.rev,
      },
    }),
  );
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() === "POST") {
      const body = r.request().postDataJSON() as { hq_contact?: string };
      state.writes.push(body);
      if (body.hq_contact) {
        state.hq_contact = body.hq_contact;
        state.rev += 1;
      }
      return r.fulfill({ json: { ...body, hq_contact_rev: state.rev } });
    }
    return r.fulfill({ json: {} });
  });
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  await mockAskConversations(page);
  return state;
}

const isPhone = (page: Page) => (page.viewportSize()?.width ?? 1440) <= 800;

async function noSidewaysScroll(page: Page) {
  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over).toBeLessThanOrEqual(0);
}

test("/ is the dashboard: Welcome, Commander., no HQ button, New primary beside the contact (#1525)", async ({
  page,
}) => {
  await mockShell(page, "southamerica-woman");
  await page.goto("/");
  await expect(page).toHaveURL(new RegExp(`${DASHBOARD_PATH}$`));
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Welcome, Commander.");
  await expect(page.getByTestId("dashboard-ask")).toHaveCount(0);

  const card = page.getByTestId("hq-primary-contact");
  await expect(card).toHaveAccessibleName(`Open HQ — ${name("southamerica-woman")}`);
  const fresh = page.getByTestId("dashboard-new");
  // New is the primary action: the accent fill with its on-accent ink, at the 44px floor. The fill
  // is the plate's face, painted by `::after` (#1531: controls paint their chamfer, never clip).
  const [bg, accent] = await fresh.evaluate((el) => [
    getComputedStyle(el, "::after").backgroundColor,
    getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
  ]);
  expect(bg).not.toBe("rgba(0, 0, 0, 0)");
  expect(accent).not.toBe("");
  const nb = (await fresh.boundingBox())!;
  const cb = (await card.boundingBox())!;
  expect(nb.height).toBeGreaterThanOrEqual(44);
  if (!isPhone(page)) {
    // Top-aligned, New directly left of the card, and the card exactly the Plan quota tile's column.
    expect(Math.abs(nb.y - cb.y)).toBeLessThanOrEqual(1);
    expect(nb.x + nb.width).toBeLessThanOrEqual(cb.x);
    const qb = (await page.getByTestId("kpi-quota").boundingBox())!;
    expect(Math.abs(cb.x - qb.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(cb.width - qb.width)).toBeLessThanOrEqual(1);
  } else {
    // ≤800px: a full-width row under the heading.
    expect(cb.y).toBeGreaterThan(nb.y + nb.height - 1);
  }
  await noSidewaysScroll(page);
});

test("/new opens the new-session landing (#1525)", async ({ page }) => {
  await mockShell(page, "random");
  await page.goto(NEW_SESSION_PATH);
  await expect(page).toHaveURL(new RegExp(`${NEW_SESSION_PATH}$`));
  await expect(page.getByTestId("dashboard-page")).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1, name: "Start a new session" })).toBeVisible();
});

test("the card opens HQ with focus inside; header, card and the next answer name the same contact", async ({
  page,
}) => {
  await mockShell(page, "romania-woman");
  await page.route(ASK_STREAM, (r) => fulfillAsk(r, { answer: "All quiet, Commander." }));
  await page.goto(DASHBOARD_PATH);
  const card = page.getByTestId("hq-primary-contact");
  await card.focus();
  await page.keyboard.press("Enter");
  const panel = page.getByTestId("ask-sidebar");
  await expect(panel).toHaveAttribute("data-open", "true");
  await expect
    .poll(() => panel.evaluate((el) => el.contains(document.activeElement)))
    .toBe(true);
  await expect(panel.getByTestId("hq-head-name")).toHaveText(name("romania-woman"));
  await expect(panel.getByTestId("hq-head-portrait")).toHaveAttribute("data-contact", "romania-woman");
  await panel.getByTestId("composer-input").fill("status?");
  await panel.getByTestId("composer-send").click();
  await expect(
    panel.getByRole("article", { name: `Answer from ${name("romania-woman")}` }),
  ).toContainText("All quiet, Commander.");
  await noSidewaysScroll(page);
});

test("switching contacts relabels new answers only (#1525)", async ({ page }) => {
  const state = await mockShell(page, "us-man");
  await page.route(ASK_STREAM, (r) => fulfillAsk(r, { answer: "Roger." }));
  await page.goto(DASHBOARD_PATH);
  await page.getByTestId("hq-primary-contact").click();
  const panel = page.getByTestId("ask-sidebar");
  await panel.getByTestId("composer-input").fill("one");
  await panel.getByTestId("composer-send").click();
  // The SETTLED answer, not the pending one (which already carries the label): leaving earlier would
  // abort the turn's request.
  await expect(panel.getByRole("article", { name: `Answer from ${name("us-man")}` })).toContainText(
    "Roger.",
  );

  // Switch in Settings; the conversation is kept (server-side), one pick away in HQ's switcher.
  await page.goto(settingsPath("appearance"));
  const target = page.getByRole("radio").and(page.locator('[data-key="europe-woman"]'));
  await target.click();
  await expect(target).toHaveAttribute("aria-checked", "true");
  expect(state.writes).toContainEqual({ hq_contact: "europe-woman" });

  await page.goto(DASHBOARD_PATH);
  await page.getByTestId("hq-primary-contact").click();
  // HQ opens empty after a page load; the kept conversation is picked from the switcher.
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  await panel.getByTestId("composer-input").fill("two");
  await panel.getByTestId("composer-send").click();
  await expect(panel.getByTestId("ask-speaker")).toHaveText([name("us-man"), name("europe-woman")]);
});

test("the gallery is operable by keyboard and by mouse, and Select contact lands on it (#1525)", async ({
  page,
}) => {
  const state = await mockShell(page, "off");
  await page.goto(DASHBOARD_PATH);
  const placeholder = page.getByTestId("hq-primary-contact");
  await expect(placeholder).toContainText("No HQ contact");
  await expect(placeholder.locator("video, img")).toHaveCount(0);
  await placeholder.getByRole("link", { name: "Select contact" }).click();
  await expect(page).toHaveURL(/\/settings\/appearance#hq-contact$/);
  const group = page.getByRole("radiogroup", { name: "Select your primary HQ contact" });
  await expect(group).toBeInViewport();
  // The checked card (Off) has focus: the arrow keys move AND select.
  const off = group.locator('[data-key="off"]');
  await expect(off).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  const lupu = group.locator('[data-key="romania-woman"]');
  await expect(lupu).toBeFocused();
  await expect(lupu).toHaveAttribute("aria-checked", "true");
  await expect(lupu).toContainText("✓ Selected");
  await expect(lupu.locator("video")).toHaveCount(1);
  expect(state.writes).toContainEqual({ hq_contact: "romania-woman" });
  // Mouse: Random.
  await group.locator('[data-key="random"]').click();
  await expect(group.locator('[data-key="random"]')).toHaveAttribute("aria-checked", "true");
  // Every card is a 44px+ target, square, and the gallery never scrolls sideways.
  for (const box of await group.getByRole("radio").evaluateAll((els) =>
    els.map((e) => e.getBoundingClientRect().height),
  ))
    expect(box).toBeGreaterThanOrEqual(44);
  await noSidewaysScroll(page);
});

test("Off: no header portrait; the dashboard's placeholder opens HQ (#1525)", async ({ page }) => {
  await mockShell(page, "off");
  await page.goto(DASHBOARD_PATH);
  const placeholder = page.getByTestId("hq-primary-contact");
  await placeholder.getByRole("button", { name: "Open HQ →" }).click();
  const panel = page.getByTestId("ask-sidebar");
  await expect(panel).toHaveAttribute("data-open", "true");
  await expect(panel.getByTestId("ask-head")).toHaveAttribute("data-contact", "off");
  await expect(panel.getByTestId("hq-head-portrait")).toHaveCount(0);
  await expect(panel.getByRole("heading", { level: 2 })).toHaveText("HQ");
  await noSidewaysScroll(page);
});

test("Random shows the day's contact under a fixed clock (#1525)", async ({ page }) => {
  const fixed = new Date(2026, 9, 20, 12, 0, 0);
  await page.clock.setFixedTime(fixed);
  await mockShell(page, "random");
  await page.goto(DASHBOARD_PATH);
  const pick = contactForDay(fixed);
  await expect(page.getByTestId("hq-primary-contact")).toHaveAttribute("data-contact", pick.id);
  await page.goto(settingsPath("appearance"));
  await expect(page.getByTestId("hq-random-pick")).toHaveText(`Today: ${pick.name}`);
});

test("reduced motion shows a still; a paused and a failed loop show the poster (#1525)", async ({
  page,
}) => {
  await mockShell(page, "europe-man");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(DASHBOARD_PATH);
  const still = page.getByTestId("hq-primary-portrait");
  await expect(still).toHaveAttribute("data-state", "still");
  await expect(page.getByTestId("hq-primary-contact").locator("video")).toHaveCount(0);

  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.reload();
  await expect(page.getByTestId("hq-primary-portrait")).toHaveAttribute("data-state", "playing");
  // The panel's portrait is paused while the panel is closed: open it, close it, look.
  await page.getByTestId("hq-primary-contact").click();
  const panel = page.getByTestId("ask-sidebar");
  await expect(panel.getByTestId("hq-head-portrait")).toHaveAttribute("data-state", "playing");
  await panel.getByRole("button", { name: "Close HQ" }).click();
  const head = panel.getByTestId("hq-head-portrait");
  await expect(head).toHaveAttribute("data-state", "paused");
  expect(await head.evaluate((v) => (v as HTMLVideoElement).paused)).toBe(true);
  expect(await head.getAttribute("poster")).toBe("/hq/europe-man.jpg");

  // Failed: both loop files 404.
  await page.route(/\/hq\/europe-man\.(webm|mp4)$/, (r) => r.fulfill({ status: 404, body: "" }));
  await page.reload();
  const failed = page.getByTestId("hq-primary-portrait");
  await expect(failed).toHaveAttribute("data-state", "failed");
  await expect(failed).toHaveAttribute("src", "/hq/europe-man.jpg");
});
