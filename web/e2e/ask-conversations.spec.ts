/** Ask's durable conversations (#1443 Phase 1), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that keyboard focus really moves into the switcher's list and
 *  back, that Escape closes ONLY the list (on a desktop the panel's own Escape closes Ask; in the
 *  phone drawer the drawer's Escape would), that the focused row's ring is visible, that the thread
 *  is scrolled to its newest entry after a switch, and that a long title neither pushes the head
 *  past the panel nor scrolls the page sideways on a phone. HQ opens on an EMPTY conversation, and
 *  Delete removes the one on screen only after its confirm dialog, which is keyboard-operable and
 *  hands focus back.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk, pickConversation } from "./askSidebar";
import { mockAskConversations, turnOf, type FakeAsk, type FakeConversation } from "./askStream";

const LONG =
  "Why does the flaky upload retry spec in web/e2e keep timing out on the shared runner overnight";

function conversations(): FakeConversation[] {
  const latest: FakeConversation = {
    id: "ask_" + "a".repeat(32),
    created_at: 10,
    updated_at: 30,
    turns: [turnOf(LONG, { answer: "The retry waits on a 5 s default timeout." })],
  };
  // Long enough that its thread scrolls: the newest entry is below the fold until scrolled to.
  const older: FakeConversation = {
    id: "ask_" + "b".repeat(32),
    created_at: 5,
    updated_at: 20,
    turns: Array.from({ length: 12 }, (_, i) =>
      turnOf(`older question ${i + 1}`, {
        answer: `older answer ${i + 1}. `.repeat(6).trim(),
      }),
    ),
  };
  const oldest: FakeConversation = {
    id: "ask_" + "c".repeat(32),
    created_at: 1,
    updated_at: 10,
    turns: [turnOf("the oldest question", { answer: "the oldest answer" })],
  };
  return [latest, older, oldest];
}

async function mockShell(page: Page): Promise<FakeAsk> {
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
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
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
  return mockAskConversations(page, conversations());
}

/** Is the thread scrolled to its newest entry? */
async function atBottom(page: Page): Promise<boolean> {
  return page.getByTestId("ask-pane").evaluate(
    (el) => el.scrollHeight - el.scrollTop - el.clientHeight <= 2,
  );
}

test("the switcher is keyboard-operable and a switch lands on the newest entry (#1443)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  // HQ opens on an empty conversation; nothing kept is reopened on its own.
  await expect(panel.getByTestId("ask-turns")).toHaveCount(0);
  const trigger = panel.getByTestId("ask-switcher");
  await expect(trigger).toContainText("New conversation");

  // Focus the switcher from the keyboard's side and open it: focus moves INTO the list, onto the
  // first (latest) row; Enter opens it.
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = panel.getByTestId("ask-switcher-menu");
  await expect(menu).toBeVisible();
  const items = menu.getByTestId("ask-switcher-item");
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toBeFocused();
  await expect(items.nth(0)).toHaveAttribute("aria-selected", "false");
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect(panel.getByTestId("ask-turns")).toContainText("5 s default timeout");
  await expect(trigger).toContainText("flaky upload retry");

  // Opened again, focus lands on the conversation on screen.
  await page.keyboard.press("Enter");
  await expect(menu).toBeVisible();
  await expect(items.nth(0)).toBeFocused();
  await expect(items.nth(0)).toHaveAttribute("aria-selected", "true");
  // The ring is visible on the focused row.
  expect(
    await items.nth(0).evaluate((el) => getComputedStyle(el).outlineStyle),
  ).not.toBe("none");

  // Arrow keys move; Enter opens the older conversation.
  await page.keyboard.press("ArrowDown");
  await expect(items.nth(1)).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(panel.getByTestId("ask-turn")).toHaveCount(12);
  await expect(panel.getByTestId("ask-turns")).toContainText("older answer 12");
  await expect(trigger).toContainText("older question 1");
  // …and the thread shows its NEWEST entry, not the top of the conversation.
  await expect.poll(() => atBottom(page)).toBe(true);
  // Read-back answers show whole at once (they are history), so nothing grows the thread after the
  // scroll landed: the newest answer is on screen and stays there.
  const newest = panel.getByTestId("ask-answer").last();
  await expect(newest).toContainText("older answer 12.");
  await expect(newest).not.toHaveAttribute("data-revealing", "true");
  await expect(newest).toBeInViewport();

  // Escape closes the list and hands focus back — and closes nothing else.
  await page.keyboard.press("Enter");
  await expect(menu).toBeVisible();
  await expect(items.nth(1)).toBeFocused();
  await page.keyboard.press("End");
  await expect(items.nth(2)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(panel).toHaveAttribute("data-open", "true");
  await expect(panel.getByTestId("ask-turn")).toHaveCount(12);
});

test("New conversation starts an empty thread and keeps the old one in the switcher (#1443)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  await panel.getByRole("button", { name: "New conversation" }).click();
  await expect(panel.getByTestId("ask-turn")).toHaveCount(0);
  await expect(panel.getByTestId("composer-input")).toBeFocused();
  await panel.getByTestId("ask-switcher").click();
  await expect(panel.getByTestId("ask-switcher-item")).toHaveCount(3);
  await expect(panel.getByTestId("ask-switcher-item").first()).toContainText("flaky upload retry");
});

test("a long title stays inside the head; nothing scrolls sideways on a phone (#1443)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  const trigger = panel.getByTestId("ask-switcher");
  await expect(trigger).toContainText("flaky upload retry");
  const head = await panel.getByTestId("ask-head").boundingBox();
  const sw = await trigger.boundingBox();
  const close = await panel.getByRole("button", { name: "Close HQ" }).boundingBox();
  expect(sw && head && close).toBeTruthy();
  // The switcher shrinks; Delete, New conversation and Close keep their place on the same row.
  expect(sw!.x + sw!.width).toBeLessThanOrEqual(close!.x);
  expect(close!.x + close!.width).toBeLessThanOrEqual(head!.x + head!.width + 1);
  expect(sw!.height).toBeGreaterThanOrEqual(44);
  await trigger.click();
  const menu = await panel.getByTestId("ask-switcher-menu").boundingBox();
  const box = await panel.boundingBox();
  expect(menu!.x).toBeGreaterThanOrEqual(box!.x - 1);
  expect(menu!.x + menu!.width).toBeLessThanOrEqual(box!.x + box!.width + 1);
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
});

test("Delete asks first, by keyboard; confirmed, the conversation is gone and HQ is empty again", async ({
  page,
}) => {
  const fake = await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const remove = panel.getByRole("button", { name: "Delete this conversation" });
  // Nothing open, nothing to delete.
  await expect(remove).toBeDisabled();
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-turns")).toContainText("5 s default timeout");
  await expect(remove).toBeEnabled();
  const head = await panel.getByTestId("ask-head").boundingBox();
  const box = await remove.boundingBox();
  // A real target on the head's row (§8: 44 px).
  expect(box!.width).toBeGreaterThanOrEqual(44);
  expect(box!.height).toBeGreaterThanOrEqual(44);
  expect(box!.y).toBeGreaterThanOrEqual(head!.y - 1);
  expect(box!.y + box!.height).toBeLessThanOrEqual(head!.y + head!.height + 1);

  // Escape cancels: nothing deleted, focus back on Delete.
  await remove.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: /^Delete “/ });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("flaky upload retry");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(remove).toBeFocused();
  // The dialog's Escape went no further: HQ stays open with its conversation.
  await expect(panel).toHaveAttribute("data-open", "true");
  await expect(panel.getByTestId("ask-turns")).toContainText("5 s default timeout");
  expect(fake.deleted ?? []).toEqual([]);

  // Confirmed from the keyboard: Tab from Cancel to Delete, Enter.
  await page.keyboard.press("Enter");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Delete" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveCount(0);
  expect(fake.deleted).toEqual(["ask_" + "a".repeat(32)]);
  await expect(panel.getByTestId("ask-turns")).toHaveCount(0);
  await expect(panel.getByTestId("ask-switcher")).toContainText("New conversation");
  await expect(remove).toBeDisabled();
  await panel.getByTestId("ask-switcher").click();
  const items = panel.getByTestId("ask-switcher-item");
  await expect(items).toHaveCount(2);
  await expect(items.first()).toContainText("older question 1");
});

test("a refused delete says why in the dialog and keeps the conversation", async ({ page }) => {
  const fake = await mockShell(page);
  fake.busy = new Set(["ask_" + "a".repeat(32)]);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await panel.getByRole("button", { name: "Delete this conversation" }).click();
  const dialog = page.getByRole("dialog", { name: /^Delete “/ });
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(dialog.getByTestId("ask-delete-error")).toContainText("still answering");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(panel.getByTestId("ask-turns")).toContainText("5 s default timeout");
  expect(fake.deleted ?? []).toEqual([]);
});
