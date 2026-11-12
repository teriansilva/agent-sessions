/** Settings → AI → HQ connectors, messenger recipients and the send switch (#1832), in a real
 *  browser on desktop and a phone, against a stateful fake that enforces the server's rules
 *  (`messengerFixture.ts`). Nothing is sent in this phase. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { settingsPath } from "../src/routes/settingsTabs";
import { BOT_TOKEN, OTHER_BOT_TOKEN, messenger, mockMessengers } from "./messengerFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const CHATS = [
  { id: "42", label: "Marcus (@marcus)", kind: "private chat" },
  { id: "-1009", label: "Release crew", kind: "supergroup" },
];

async function open(page: Page) {
  await page.goto(settingsPath("ai-hq-connectors"));
  await expect(page.getByTestId("connectors-catalog")).toBeVisible();
}

const card = (page: Page) => page.getByTestId("connector-card");

async function checks(page: Page, scope: Locator) {
  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over).toBeLessThanOrEqual(0);
  if (!PHONE(page)) return;
  for (const el of await scope.locator("button:visible, input:visible").all()) {
    const target =
      (await el.getAttribute("type")) === "checkbox" ? el.locator("xpath=ancestor::label[1]") : el;
    expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

test("Telegram: test the bot, pick chats the server listed, save, then switch sending on", async ({ page }) => {
  const srv = await mockMessengers(page);
  srv.candidates = CHATS;
  await open(page);
  await page.getByRole("button", { name: "Add Telegram" }).click();
  const c = card(page);
  await expect(c.getByLabel("Address")).toHaveValue("https://api.telegram.org");
  await c.getByLabel("Access token").fill(BOT_TOKEN);
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-tested")).toHaveText("Signed in as hq_bot");
  await expect(c.getByTestId("connector-status")).toHaveText("Tested · sending is off");
  // No read controls on a connector that reads nothing.
  await expect(c.getByRole("checkbox", { name: "Let HQ read through this connector" })).toHaveCount(0);

  const rec = c.getByTestId("connector-recipients");
  const send = rec.getByRole("checkbox", { name: "Let HQ propose messages" });
  await expect(send).toBeDisabled();
  await rec.getByRole("button", { name: "Refresh" }).click();
  await expect(rec.getByTestId("connector-recipient")).toHaveCount(2);
  await rec.getByRole("checkbox", { name: /Marcus/ }).check();
  await rec.getByRole("button", { name: "Save recipients" }).click();
  expect(srv.saves).toEqual([{ ids: ["42"] }]); // ids only: the label is the server's
  await expect(send).toBeEnabled();
  await send.check();
  await expect(c.getByTestId("connector-status")).toHaveText("HQ may propose messages");
  await expect(rec.getByText(/HQ never sends a proposed message on its own/)).toBeVisible();

  // Keyboard: Tab walks the recipients, then Refresh (Save is disabled until something changes).
  await rec.getByRole("checkbox", { name: /Marcus/ }).focus();
  await page.keyboard.press("Tab");
  await expect(rec.getByRole("checkbox", { name: /Release crew/ })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(rec.getByRole("button", { name: "Refresh" })).toBeFocused();
  await checks(page, c);
});

test("discovery that finds nothing, and a bot with a webhook, say what to do", async ({ page }) => {
  const srv = await mockMessengers(page, [
    messenger({ token: BOT_TOKEN, login: "hq_bot" }),
  ]);
  await open(page);
  const rec = card(page).getByTestId("connector-recipients");
  await rec.getByRole("button", { name: "Refresh" }).click();
  await expect(rec.getByTestId("connector-recipients-empty")).toContainText(
    "No chats yet. Send any message to your bot",
  );
  srv.failList = [502, "webhook_active", "This bot has a webhook, so BattleLab can't see its chats. Use a separate bot for BattleLab."];
  await rec.getByRole("button", { name: "Refresh" }).click();
  await expect(rec.getByTestId("connector-recipients-error")).toContainText("Use a separate bot");
  await checks(page, card(page));
});

test("a save after the connector changed is refused and asks to list again", async ({ page }) => {
  const srv = await mockMessengers(page, [messenger({ token: BOT_TOKEN, login: "hq_bot" })]);
  srv.candidates = CHATS;
  await open(page);
  const rec = card(page).getByTestId("connector-recipients");
  await rec.getByRole("button", { name: "Refresh" }).click();
  await rec.getByRole("checkbox", { name: /Release crew/ }).check();
  srv.instances[0].generation += 1; // another tab replaced the token meanwhile
  await rec.getByRole("button", { name: "Save recipients" }).click();
  await expect(rec.getByTestId("connector-recipients-error")).toContainText("list the chats again");
});

test("an encrypted room can't be chosen, and a saved room that became unusable is dropped on the next save", async ({
  page,
}) => {
  const srv = await mockMessengers(page, [
    messenger({
      manifest_id: "matrix",
      origin: "https://matrix.example.org",
      token: BOT_TOKEN,
      login: "hq_bot",
      recipients: [{ id: "!ops:example.org", label: "Ops", kind: "room" }],
      bound: { origin: "https://matrix.example.org", identity: "hq_bot" },
      send: true,
    }),
  ]);
  srv.candidates = [
    { id: "!ops:example.org", label: "Ops", kind: "room", members: 4, unavailable: "encrypted, which isn't supported yet" },
    { id: "!dev:example.org", label: "Dev", kind: "room", members: 6 },
  ];
  await open(page);
  const rec = card(page).getByTestId("connector-recipients");
  await expect(rec.getByText(/Invite the bot to a room/).first()).toBeVisible();
  await rec.getByRole("button", { name: "Refresh" }).click();
  const ops = rec.getByRole("checkbox", { name: /Ops/ });
  await expect(ops).toBeDisabled();
  await expect(ops).not.toBeChecked();
  await expect(rec.getByText(/can’t be used: encrypted/)).toBeVisible();
  await rec.getByRole("checkbox", { name: /Dev/ }).check();
  await rec.getByRole("button", { name: "Save recipients" }).click();
  expect(srv.saves).toEqual([{ ids: ["!dev:example.org"] }]);
  await checks(page, card(page));
});

test("a different bot switches sending off, says why, and only a re-save brings it back", async ({ page }) => {
  const srv = await mockMessengers(page, [
    messenger({
      token: BOT_TOKEN,
      login: "hq_bot",
      recipients: [{ id: "42", label: "Marcus (@marcus)", kind: "private chat" }],
      bound: { origin: "https://api.telegram.org", identity: "hq_bot" },
      send: true,
    }),
  ]);
  srv.candidates = CHATS;
  await open(page);
  const c = card(page);
  await expect(c.getByTestId("connector-status")).toHaveText("HQ may propose messages");
  await c.getByRole("button", { name: "Replace token" }).click();
  await c.getByLabel("Access token").fill(OTHER_BOT_TOKEN);
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-tested")).toHaveText("Signed in as other_bot");
  const rec = c.getByTestId("connector-recipients");
  await expect(rec.getByTestId("connector-send-off")).toContainText(
    "Sending was switched off because this connector now signs in as other_bot",
  );
  const send = rec.getByRole("checkbox", { name: "Let HQ propose messages" });
  await expect(send).not.toBeChecked();
  await expect(send).toBeDisabled();
  await expect(rec.getByTestId("connector-recipient").getByText(/chosen for hq_bot/)).toBeVisible();

  await rec.getByRole("button", { name: "Refresh" }).click();
  await rec.getByRole("button", { name: "Save recipients" }).click(); // the same list, re-bound
  await expect(rec.getByTestId("connector-send-off")).toHaveCount(0);
  await expect(send).toBeEnabled();
  await send.check();
  await expect(c.getByTestId("connector-status")).toHaveText("HQ may propose messages");
  await checks(page, c);
});

test("a chat the new bot can't see is shown as removed, never kept out of sight (Hermes 7603)", async ({ page }) => {
  const srv = await mockMessengers(page, [
    messenger({
      token: BOT_TOKEN,
      login: "hq_bot",
      recipients: [{ id: "42", label: "Marcus (@marcus)", kind: "private chat" }],
      bound: { origin: "https://api.telegram.org", identity: "hq_bot" },
      send: true,
    }),
  ]);
  // The replacement bot has only ever seen the group.
  srv.candidates = CHATS.filter((c) => c.id !== "42");
  await open(page);
  const c = card(page);
  await c.getByRole("button", { name: "Replace token" }).click();
  await c.getByLabel("Access token").fill(OTHER_BOT_TOKEN);
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-tested")).toHaveText("Signed in as other_bot");
  const rec = c.getByTestId("connector-recipients");
  await rec.getByRole("button", { name: "Refresh" }).click();
  const marcus = rec.getByRole("checkbox", { name: /Marcus/ });
  await expect(marcus).toBeVisible();
  await expect(marcus).not.toBeChecked();
  await expect(marcus).toBeDisabled();
  await expect(rec.getByText(/this bot can’t see it, so saving removes it/)).toBeVisible();
  const group = srv.candidates[0];
  await rec.getByRole("checkbox", { name: new RegExp(group.label) }).check();
  await rec.getByRole("button", { name: "Save recipients" }).click();
  expect(srv.saves).toEqual([{ ids: [group.id] }]);
  await expect(rec.getByRole("checkbox", { name: /Marcus/ })).toHaveCount(0);
  await checks(page, c);
});
