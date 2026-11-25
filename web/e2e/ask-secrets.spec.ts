/** HQ's secret cards (#1786 P4a), in a real browser, desktop and mobile, light and dark.
 *
 *  What a DOM emulator cannot prove: that the store-secret card's write-only field is a real
 *  password input meeting the 44px floor, typed by keyboard, whose value reaches the decide
 *  request once and is gone from the page afterwards; that the access-on card's "Widens access"
 *  stamp paints in both themes; and that nothing scrolls sideways on a phone.
 */
import { expect, test } from "@playwright/test";

import { decideRoute, minTarget, noSidewaysScroll, open, PID, shell } from "./askOperationShell";

const VALUE = "card-typed-value-1786";

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "secret.create",
    risk: "reversible",
    label: "Store secret",
    verb: "Store",
    title: "deploy_token",
    text:
      "Stores a new secret “deploy_token” in the Library. Type its value below: it goes " +
      "straight to the encrypted store, and HQ never sees it. Agents get no access until you " +
      "turn that on.",
    needs: ["value"],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    ...over,
  };
}

for (const theme of ["dark", "light"] as const) {
  test(`the store-secret card takes the value on the card and sends it once (${theme})`, async ({
    page,
  }) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const fake = await shell(page, card());
    const bodies = await decideRoute(fake, page, {
      state: "done",
      summary: "Stored. Agent access is off.",
      library: { kind: "secret", id: "" },
    });
    const c = await open(page);
    const field = c.getByLabel("Value", { exact: true });
    await expect(field).toHaveAttribute("type", "password");
    await minTarget(field);
    await minTarget(c.getByTestId("ask-operation-confirm"));
    await expect(c.getByTestId("ask-operation-confirm")).toBeDisabled();
    await field.focus();
    await page.keyboard.type(VALUE);
    await expect(c.getByTestId("ask-operation-confirm")).toBeEnabled();
    // Typed, the value is the field's live value only — never mirrored into the markup.
    expect(await field.getAttribute("value")).toBeNull();
    await expect(field).toHaveAttribute("autocomplete", "new-password");
    expect(await page.evaluate((v) => document.body.innerHTML.includes(v), VALUE)).toBe(false);
    await noSidewaysScroll(page);
    await c.getByTestId("ask-operation-confirm").click();
    await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
      timeout: 20_000,
    });
    expect(bodies).toEqual([{ decision: "confirm", inputs: { value: VALUE } }]);
    await expect(c.getByLabel("Value", { exact: true })).toHaveCount(0);
    await expect(c.getByTestId("ask-library-open")).toHaveAttribute("href", "/library/variables");
    const held = await page.evaluate(
      (v) =>
        [...document.querySelectorAll("input")].some((i) => i.value.includes(v)) ||
        document.body.innerHTML.includes(v),
      VALUE,
    );
    expect(held).toBe(false);
  });

  test(`turning agent access on is stamped Widens access (${theme})`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const fake = await shell(
      page,
      card({
        op: "secret.access_on",
        risk: "external",
        label: "Turn agent access on",
        verb: "Turn on",
        title: "asc_api_key",
        text:
          "Lets agents in Atlas iOS request a private copy of “asc_api_key” through the secrets " +
          "tool. An agent can read that copy and send it anywhere.",
        needs: [],
      }),
    );
    const bodies = await decideRoute(fake, page, { state: "done", summary: "Agent access is on." });
    const c = await open(page);
    await expect(c).toContainText("Widens access");
    await expect(c).toContainText("Atlas iOS");
    await expect(c.getByLabel("Value", { exact: true })).toHaveCount(0);
    await minTarget(c.getByTestId("ask-operation-confirm"));
    await noSidewaysScroll(page);
    await c.getByTestId("ask-operation-confirm").click();
    await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", {
      timeout: 20_000,
    });
    expect(bodies).toEqual([{ decision: "confirm" }]);
  });
}

test("an unfinished move says how far it got, and Finish runs it again", async ({ page }) => {
  const fake = await shell(
    page,
    card({
      op: "secret.store_attachment",
      label: "Store shared file as secret",
      verb: "Store",
      title: "asc_api_key",
      text: "Moves the file “AuthKey_7F3K2L9QXA.p8” you shared as a secret into the Library.",
      needs: [],
      state: "unsettled",
      summary:
        "Stored in the Library as “asc_api_key”, but the chat copy could not be deleted yet. " +
        "Confirm again to finish.",
      can_confirm: true,
      can_dismiss: false,
    }),
  );
  const bodies = await decideRoute(fake, page, {
    state: "done",
    summary: "Stored in the Library as “asc_api_key”. The chat copy is deleted.",
  });
  const c = await open(page);
  const outcome = c.getByTestId("ask-operation-outcome");
  await expect(outcome).toContainText("Not finished");
  await expect(c.getByTestId("ask-operation-dismiss")).toHaveCount(0);
  const finish = c.getByTestId("ask-operation-finish");
  await minTarget(finish);
  await noSidewaysScroll(page);
  await finish.click();
  await expect(outcome).toContainText("Done", { timeout: 20_000 });
  expect(bodies).toEqual([{ decision: "confirm" }]);
});

test("the store-secret-file card sends the chosen file as one multipart part", async ({ page }) => {
  const fake = await shell(
    page,
    card({
      op: "secret.create_file",
      label: "Store secret file",
      title: "asc_api_key",
      text: "Stores a new secret file “asc_api_key” in the Library. Choose the file below.",
      needs: ["file"],
    }),
  );
  const seen: { url: string; type: string; body: string }[] = [];
  await page.route(`**/api/ask/proposals/${PID}/decide**`, (r) => {
    const req = r.request();
    seen.push({
      url: req.url(),
      type: req.headers()["content-type"] ?? "",
      body: req.postDataBuffer()?.toString("latin1") ?? "",
    });
    Object.assign(fake.conversations[0].turns[0].proposals![0], {
      state: "done",
      summary: "Stored. Agent access is off.",
      can_confirm: false,
      can_dismiss: false,
    });
    return r.fulfill({ status: 202, json: { proposal: { state: "deciding" } } });
  });
  const c = await open(page);
  await expect(c.getByTestId("ask-operation-confirm")).toBeDisabled();
  await minTarget(c.getByTestId("ask-operation-choose"));
  await c.getByLabel("Secret file").setInputFiles({
    name: "AuthKey_7F3K2L9QXA.p8",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("-----BEGIN PRIVATE KEY-----\nMIGT\n-----END PRIVATE KEY-----\n"),
  });
  await expect(c.getByTestId("ask-operation-file")).toContainText("AuthKey_7F3K2L9QXA.p8");
  await noSidewaysScroll(page);
  await c.getByTestId("ask-operation-confirm").click();
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Done", { timeout: 20_000 });
  expect(seen).toHaveLength(1);
  expect(seen[0].url).toContain("decision=confirm");
  expect(seen[0].type).toContain("multipart/form-data");
  expect(seen[0].body).toContain('filename="AuthKey_7F3K2L9QXA.p8"');
});
