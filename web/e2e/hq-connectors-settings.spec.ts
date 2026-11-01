/** Settings → AI → HQ connectors (#1825, #1746 P1b), in a real browser on desktop and a phone,
 *  against a stateful fake of `/api/connectors` that enforces the store's rules
 *  (`connectorsFixture.ts`). */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { settingsPath } from "../src/routes/settingsTabs";
import { CLASSIC_TOKEN, GOOD_TOKEN, instance, mockConnectors } from "./connectorsFixture";
import { TOUCH_FLOOR } from "./touchTarget";

const PHONE = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;
const tested = { login: "commander", repos: 7, at: 2 };

async function open(page: Page) {
  await page.goto(settingsPath("ai-hq-connectors"));
  await expect(page.getByTestId("connectors-catalog")).toBeVisible();
}

const card = (page: Page) => page.getByTestId("connector-card");

async function noSideways(page: Page) {
  const over = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(over).toBeLessThanOrEqual(0);
}

async function touchTargets(scope: Locator) {
  for (const el of await scope.locator("button:visible, input:visible").all()) {
    const target = (await el.getAttribute("type")) === "checkbox" ? el.locator("xpath=ancestor::label[1]") : el;
    const box = (await target.boundingBox())!;
    expect(box.height, (await el.textContent()) || (await el.getAttribute("id")) || "").toBeGreaterThanOrEqual(
      TOUCH_FLOOR,
    );
  }
}

test("add, save and test, then switch reading on — and the token never comes back", async ({ page }) => {
  const srv = await mockConnectors(page);
  await open(page);
  await expect(page.getByTestId("connectors-empty")).toBeVisible();

  await page.getByRole("button", { name: "Add Forgejo" }).click();
  const c = card(page);
  await expect(c).toHaveCount(1);
  await expect(c.getByTestId("connector-status")).toHaveText("Needs its address");
  // The new card's address takes focus, so setup starts where it has to.
  await expect(c.getByLabel("Address")).toBeFocused();

  const save = c.getByRole("button", { name: "Save and test" });
  await c.getByLabel("Address").fill("https://git.example.com");
  await expect(save).toBeDisabled(); // no token yet
  await expect(c.getByText("read:issue, read:repository")).toBeVisible();
  await c.getByLabel("Access token").fill(GOOD_TOKEN);
  const read = c.getByRole("checkbox", { name: "Let HQ read through this connector" });
  await expect(read).toBeDisabled();
  await save.click();

  await expect(c.getByTestId("connector-tested")).toHaveText(/Signed in as commander · 7 repositories visible/);
  await expect(c.getByTestId("connector-status")).toHaveText("Tested · reading is off");
  expect(srv.patches).toEqual([{ origin: "https://git.example.com", token: GOOD_TOKEN }]);
  // Write-only: the field is gone, nothing on the page holds the value, and it is marked saved.
  await expect(c.getByLabel("Access token")).toHaveCount(0);
  await expect(c.getByText("saved", { exact: true })).toBeVisible();
  const values = await page.locator("input").evaluateAll((els) => els.map((e) => (e as HTMLInputElement).value));
  expect(values).not.toContain(GOOD_TOKEN);
  expect(await page.content()).not.toContain(GOOD_TOKEN);

  await expect(read).toBeEnabled();
  await expect(c.getByTestId("connector-disclosure")).toContainText("https://api.example.com");
  await read.check();
  await expect(c.getByTestId("connector-status")).toHaveText("HQ reads through it");
  await expect(read).toBeChecked();
  await expect(c.getByRole("button", { name: "Test again" })).toBeVisible();

  if (PHONE(page)) {
    await touchTargets(c);
    await touchTargets(page.getByTestId("connectors-catalog"));
  }
  await noSideways(page);
});

test("a rejected token shows the forge's words and keeps reading off", async ({ page }) => {
  await mockConnectors(page, [instance({ origin: "https://git.example.com" })]);
  await open(page);
  const c = card(page);
  await expect(c.getByTestId("connector-status")).toHaveText("Needs a token");
  await c.getByLabel("Access token").fill("expired-token");
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-test-failed")).toHaveText("git.example.com rejected the token (401)");
  await expect(c.getByTestId("connector-status")).toHaveText("Test failed");
  await expect(c.getByRole("checkbox", { name: "Let HQ read through this connector" })).toBeDisabled();

  // After a reload only the stored code is left, and it still reads as words.
  await page.reload();
  await expect(card(page).getByTestId("connector-test-failed")).toContainText("The forge refused the token");
});

test("a failed re-test stops reading, says so, and survives a reload; a passing one resumes it", async ({
  page,
}) => {
  const srv = await mockConnectors(page, [
    instance({ origin: "https://git.example.com", token: GOOD_TOKEN, tested, read: true }),
  ]);
  await open(page);
  const c = card(page);
  const read = c.getByRole("checkbox", { name: "Let HQ read through this connector" });
  await expect(c.getByTestId("connector-status")).toHaveText("HQ reads through it");
  await expect(read).toBeChecked();

  srv.revoked = true; // the token was revoked on the forge
  await c.getByRole("button", { name: "Test again" }).click();
  await expect(c.getByTestId("connector-test-failed")).toHaveText("git.example.com rejected the token (401)");
  await expect(c.getByTestId("connector-status")).toHaveText("Test failed");
  await expect(read).not.toBeChecked();
  await expect(read).toBeDisabled();
  await expect(c.getByText("HQ can’t read through it until a test passes.")).toBeVisible();

  await page.reload();
  const again = card(page);
  await expect(again.getByTestId("connector-status")).toHaveText("Test failed");
  await expect(again.getByRole("checkbox", { name: "Let HQ read through this connector" })).not.toBeChecked();
  await expect(again.getByTestId("connector-test-failed")).toContainText("The forge refused the token");

  srv.revoked = false;
  await again.getByRole("button", { name: "Test", exact: true }).click();
  await expect(again.getByTestId("connector-status")).toHaveText("HQ reads through it");
});

test("an older listing that arrives late never undoes a newer read switch (Hermes on #1828)", async ({
  page,
}) => {
  const srv = await mockConnectors(page, [
    instance({ id: "c_aaaaaaaaaaaa", origin: "https://git.example.com", token: GOOD_TOKEN, tested }),
    instance({ id: "c_bbbbbbbbbbbb", origin: "https://git.example.org", token: GOOD_TOKEN, tested }),
  ]);
  await open(page);
  const [a, b] = [card(page).nth(0), card(page).nth(1)];
  const readB = b.getByRole("checkbox", { name: "Let HQ read through this connector" });

  // Card A's re-test: its refresh is taken now (B reading off) and held.
  let release!: () => void;
  srv.holdList = new Promise<void>((r) => (release = r));
  await a.getByRole("button", { name: "Test again" }).click();
  // A stays busy until its own refresh lands.
  await expect(a.getByRole("button", { name: "Testing…" })).toBeDisabled();

  // Meanwhile B switches reading on; its refresh lands at once.
  await readB.check();
  await expect(b.getByTestId("connector-status")).toHaveText("HQ reads through it");

  // The older snapshot arrives last. It must not put B back to "reading is off".
  release();
  await expect(a.getByRole("button", { name: "Test again" })).toBeEnabled();
  await expect(readB).toBeChecked();
  await expect(b.getByTestId("connector-status")).toHaveText("HQ reads through it");
});

test("a new address needs the token again, and both travel in one save", async ({ page }) => {
  const srv = await mockConnectors(page, [
    instance({ origin: "https://git.example.com", token: GOOD_TOKEN, tested, read: true }),
  ]);
  await open(page);
  const c = card(page);
  await expect(c.getByTestId("connector-status")).toHaveText("HQ reads through it");

  await c.getByLabel("Address").fill("https://git.example.org");
  await expect(c.getByRole("note")).toContainText("A new address needs the token again");
  const save = c.getByRole("button", { name: "Save and test" });
  await expect(save).toBeDisabled();

  await c.getByRole("button", { name: "Replace token" }).click();
  await c.getByLabel("Access token").fill(GOOD_TOKEN);
  await expect(c.getByRole("note")).toHaveCount(0);
  await save.click();
  await expect(c.getByTestId("connector-tested")).toBeVisible();
  expect(srv.patches).toEqual([{ origin: "https://git.example.org", token: GOOD_TOKEN }]);
  // A changed connector is untested on the server, so reading went off with the change.
  await expect(c.getByTestId("connector-status")).toHaveText("Tested · reading is off");
});

test("a test the connector outran says so and asks for another", async ({ page }) => {
  const srv = await mockConnectors(page, [instance({ origin: "https://git.example.com", token: GOOD_TOKEN })]);
  await open(page);
  const c = card(page);
  let release!: () => void;
  srv.holdTest = new Promise<void>((r) => (release = r));
  await c.getByRole("button", { name: "Test", exact: true }).click();
  await expect(c.getByRole("button", { name: "Testing…" })).toBeDisabled();
  // Another tab changes the connector while the test is in flight.
  srv.instances[0].generation += 1;
  release();
  await expect(c.getByRole("status")).toHaveText(
    "The connector changed while it was being tested. Test it again.",
  );
  await expect(c.getByTestId("connector-status")).toHaveText("Needs a test");
});

test("remove asks first, and Keep it keeps it", async ({ page }) => {
  const srv = await mockConnectors(page, [
    instance({ origin: "https://git.example.com", token: GOOD_TOKEN, tested }),
  ]);
  await open(page);
  const c = card(page);
  await c.getByRole("button", { name: "Remove", exact: true }).click();
  const confirm = c.getByRole("group", { name: "Confirm removing Forgejo" });
  await confirm.getByRole("button", { name: "Keep it" }).click();
  await expect(card(page)).toHaveCount(1);

  await c.getByRole("button", { name: "Remove", exact: true }).click();
  if (PHONE(page)) await touchTargets(confirm);
  await confirm.getByRole("button", { name: "Remove connector" }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByTestId("connectors-empty")).toBeVisible();
  expect(srv.instances).toHaveLength(0);
});

test("GitHub's address is fixed, and a classic token that can write passes with a warning (#1826)", async ({
  page,
}) => {
  const srv = await mockConnectors(page);
  await open(page);
  await page.getByRole("button", { name: "Add GitHub" }).click();
  const c = card(page);
  await expect(c.getByLabel("Address")).toBeDisabled();
  await expect(c.getByLabel("Address")).toHaveValue("https://api.github.com");
  await expect(c.getByTestId("connector-status")).toHaveText("Needs a token");
  await c.getByLabel("Access token").fill(CLASSIC_TOKEN);
  await c.getByRole("button", { name: "Save and test" }).click();
  await expect(c.getByTestId("connector-tested")).toHaveText(/Signed in as commander/);
  await expect(c.getByTestId("connector-test-warning")).toContainText("This token can also write (repo)");
  // The fixed origin never travels in the PATCH: only the token does.
  expect(srv.patches).toEqual([{ token: CLASSIC_TOKEN }]);
  if (PHONE(page)) await touchTargets(c);
  await noSideways(page);
});

test("a Nextcloud connector signs in with an account and an app password; a new account needs it again", async ({
  page,
}) => {
  const srv = await mockConnectors(page);
  await open(page);
  await page.getByRole("button", { name: "Add Nextcloud" }).click();
  const c = card(page);
  await expect(c.getByLabel("Address")).toBeFocused();
  await c.getByLabel("Address").fill("https://cloud.example.com");
  const save = c.getByRole("button", { name: "Save and test" });
  await c.getByLabel("App password").fill(GOOD_TOKEN);
  await expect(save).toBeDisabled(); // no account yet
  await c.getByLabel("Account").fill("anna");
  await save.click();
  await expect(c.getByTestId("connector-tested")).toHaveText(/Signed in as anna@example.com · 4 items in your files/);
  expect(srv.patches).toEqual([{ origin: "https://cloud.example.com", account: "anna", token: GOOD_TOKEN }]);
  await expect(c.getByLabel("App password")).toHaveCount(0);
  expect(await page.content()).not.toContain(GOOD_TOKEN);

  // A new account: the saved password is only ever sent for the account it was saved with.
  await c.getByLabel("Account").fill("bob");
  await expect(c.getByRole("note")).toContainText("A new account needs the app password again");
  await expect(save).toBeDisabled();
  await c.getByRole("button", { name: "Replace app password" }).click();
  await c.getByLabel("App password").fill(GOOD_TOKEN);
  await save.click();
  await expect(c.getByTestId("connector-tested")).toBeVisible();
  expect(srv.patches.at(-1)).toEqual({ origin: "https://cloud.example.com", account: "bob", token: GOOD_TOKEN });

  if (PHONE(page)) await touchTargets(c);
  await noSideways(page);
});
