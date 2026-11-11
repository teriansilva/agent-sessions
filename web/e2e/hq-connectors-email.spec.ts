/** Settings → AI → HQ connectors, the email connector (#1836): its From address, the addresses HQ
 *  may write to, and the send switch, in a real browser on desktop and a phone, against a stateful
 *  fake that enforces the server's rules (`emailFixture.ts`). Nothing is sent here; the transport
 *  is pinned by `tests/test_connectors_smtp.py`. */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { settingsPath } from "../src/routes/settingsTabs";
import { PASSWORD, mailbox, mockEmail } from "./emailFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const ORIGIN = "smtps://mail.example.com";

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

function tested(over = {}) {
  return mailbox({
    origin: ORIGIN,
    account: "hq@example.com",
    password: PASSWORD,
    login: "hq@example.com",
    generation: 3,
    ...over,
  });
}

test("set up email: the server, the account, a From address, addresses, then sending", async ({ page }) => {
  const srv = await mockEmail(page);
  await open(page);
  await page.getByRole("button", { name: "Add Email" }).click();
  const c = card(page);
  await expect(c.getByLabel("Address", { exact: true })).toHaveAttribute("placeholder", ORIGIN);
  await c.getByLabel("Address", { exact: true }).fill(ORIGIN);
  await c.getByLabel("Account").fill("hq@example.com");
  await c.getByLabel("App password").fill(PASSWORD);
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-tested")).toHaveText("Signed in as hq@example.com");

  const rec = c.getByTestId("connector-recipients");
  const send = rec.getByRole("checkbox", { name: "Let HQ propose mail" });
  await expect(send).toBeDisabled();
  await rec.getByLabel("From address").fill("release-bot@example.com");
  await rec.getByTestId("connector-sender-save").click();
  await expect(rec.getByLabel("From address")).toHaveValue("release-bot@example.com");
  await expect(c.getByTestId("connector-tested")).toHaveText("Signed in as hq@example.com");
  expect(srv.patches.at(-1)).toEqual({ sender: "release-bot@example.com" });

  await rec.getByLabel("Add an address").fill("Crew@Example.COM");
  await rec.getByLabel("Name for this address (optional)").fill("Release crew");
  await rec.getByRole("button", { name: "Add", exact: true }).click();
  const row = rec.getByTestId("connector-recipient");
  await expect(row).toHaveCount(1);
  // Visibly unsaved until the server takes it; the domain already shows as it will be stored.
  await expect(row).toHaveAttribute("data-unsaved", "true");
  await expect(row).toContainText("Release crew · Crew@example.com · not saved yet");
  await expect(send).toBeDisabled();
  await checks(page, rec);
  await rec.getByRole("button", { name: "Save addresses" }).click();
  await expect(row).not.toHaveAttribute("data-unsaved", "true");
  await expect(row).toContainText("Release crew · Crew@example.com");
  expect(srv.saves).toEqual([
    { addresses: [{ address: "Crew@example.com", label: "Release crew" }], generation: 3 },
  ]);
  await expect(send).toBeEnabled();
  await send.check();
  await expect(c.getByTestId("connector-status")).toHaveText("HQ may propose messages");
  await expect(rec.getByText("mail can’t be taken back once it’s sent", { exact: false })).toBeVisible();
  await checks(page, rec);
});

for (const bad of [
  "Release Crew <crew@example.com>",
  "crew@example.com, ops@example.com",
  "crew example.com",
  "crew@localhost",
]) {
  test(`an invalid address (${bad}) is refused in words and nothing is added or saved`, async ({ page }) => {
    const srv = await mockEmail(page, [tested({ sender: "release-bot@example.com" })]);
    await open(page);
    const rec = card(page).getByTestId("connector-recipients");
    await rec.getByLabel("Add an address").fill(bad);
    await rec.getByRole("button", { name: "Add", exact: true }).click();
    await expect(rec.getByTestId("connector-address-invalid")).toContainText(bad);
    await expect(rec.getByTestId("connector-address-invalid")).toContainText("name@example.com");
    await expect(rec.getByTestId("connector-recipient")).toHaveCount(0);
    await expect(rec.getByRole("button", { name: "Save addresses" })).toBeDisabled();
    expect(srv.saves).toEqual([]);
    await checks(page, rec);
  });
}

test("a saved address is removed with Remove and the list is saved without it", async ({ page }) => {
  const srv = await mockEmail(page, [
    tested({
      sender: "release-bot@example.com",
      recipients: [
        { id: "crew@example.com", label: "Release crew", kind: "email" },
        { id: "ops@example.com", label: "ops@example.com", kind: "email" },
      ],
      bound: { origin: ORIGIN, identity: "hq@example.com" },
      send: true,
    }),
  ]);
  await open(page);
  const rec = card(page).getByTestId("connector-recipients");
  await expect(rec.getByTestId("connector-recipient")).toHaveCount(2);
  await checks(page, rec);
  await rec.getByRole("button", { name: "Remove ops@example.com" }).click();
  await expect(rec.getByTestId("connector-recipient")).toHaveCount(1);
  await rec.getByRole("button", { name: "Save addresses" }).click();
  expect(srv.saves).toEqual([
    { addresses: [{ address: "crew@example.com", label: "Release crew" }], generation: 3 },
  ]);
});

test("after the account changes, sending is off with the reason and the addresses are flagged until saved", async ({ page }) => {
  await mockEmail(page, [
    tested({
      account: "ops@example.com",
      login: "ops@example.com",
      sender: "release-bot@example.com",
      recipients: [{ id: "crew@example.com", label: "Release crew", kind: "email" }],
      bound: { origin: ORIGIN, identity: "hq@example.com" },
      send: false,
      send_off: { reason: "identity_changed", was: "hq@example.com", now: "ops@example.com", at: 2 },
    }),
  ]);
  await open(page);
  const rec = card(page).getByTestId("connector-recipients");
  await expect(rec.getByTestId("connector-send-off")).toContainText("now signs in as ops@example.com");
  await expect(rec.getByTestId("connector-send-off")).toContainText("chosen for hq@example.com");
  const send = rec.getByRole("checkbox", { name: "Let HQ propose mail" });
  await expect(send).not.toBeChecked();
  await expect(send).toBeDisabled();
  await expect(rec.getByTestId("connector-recipient")).toContainText("chosen for hq@example.com");
  await checks(page, rec);
  // Saving the same list again is the new consent.
  await rec.getByRole("button", { name: "Save addresses" }).click();
  await expect(rec.getByTestId("connector-send-off")).toHaveCount(0);
  await expect(rec.getByTestId("connector-recipient")).not.toContainText("chosen for");
  await expect(send).toBeEnabled();
});
