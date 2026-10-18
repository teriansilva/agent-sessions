import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, ID, QUEUED, clients, mockStructuredGuidance } from "./structuredGuidanceFixture";

// Remove a queued message, and Always send now (#1447). Real browser: hit areas, wrapping on the
// phone, and the automatic Send now that follows the queue without a click.
const LATER = "33333333-2222-4333-8444-555555555555";

async function queue(page: Page, engine: string, mode: string, theme = "dark") {
  const fixture = await mockStructuredGuidance(page, engine, mode, theme);
  const queued = fixture.state.turns[1];
  fixture.state.turns.push({ ...queued, turn_id: LATER, operation_id: LATER,
    text: "Then summarise what you found.", ts: (queued.ts as number) + 60 });
  await page.goto(`/s/${engine}/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  return fixture;
}

const row = (page: Page, text: string) =>
  page.getByTestId("structured-turn").filter({ hasText: text });

for (const theme of ["dark", "light"]) {
  test(`Remove takes a queued message out and keeps the rest (${theme})`, async ({ page }, testInfo) => {
    const fixture = await queue(page, "claude-api", "interrupt", theme);
    const first = row(page, "Only inspect the files.");
    const remove = first.getByRole("button", { name: "Remove this queued message" });
    const box = (await remove.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(44);
    // Every control sits inside the pane: nothing pushed past the edge on a phone.
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    await page.screenshot({ path: testInfo.outputPath("queue.png") });
    await remove.click();
    await expect.poll(() => fixture.withdrawals.length).toBe(1);
    expect(fixture.withdrawals[0]).toMatchObject({ queued_turn_id: QUEUED });
    expect(fixture.withdrawals[0].operation_id).toMatch(/^[0-9a-f-]{36}$/);
    await expect(first).toHaveCount(0);
    // The next message moves up to the head of the queue.
    await expect(row(page, "Then summarise what you found.")).toContainText("1st in queue");
    expect(fixture.calls).toHaveLength(0); // removing never sends anything
  });
}

test("a refused Remove explains itself and leaves the message queued", async ({ page }) => {
  const fixture = await queue(page, "codex-api", "steer");
  fixture.state.turns.splice(1, 1); // sent meanwhile, but this tab still shows it
  await page.route(/\/withdraw$/, (r) => {
    fixture.withdrawals.push(r.request().postDataJSON());
    return r.fulfill({ status: 409, json: { detail: "the message was already sent or is no longer queued" } });
  });
  await row(page, "Then summarise what you found.").getByRole("button", { name: "Remove this queued message" }).click();
  await expect(page.getByTestId("structured-error")).toContainText("Not removed: the message was already sent");
});

test("an older worker offers no Remove", async ({ page }) => {
  const fixture = await mockStructuredGuidance(page, "claude-api", "interrupt");
  fixture.state.native.withdraw = false;
  await page.goto(`/s/claude-api/${ID}`);
  await expect(row(page, "Only inspect the files.").getByRole("button", { name: "Send now", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove this queued message" })).toHaveCount(0);
});

for (const [engine, mode] of clients) {
  test(`${engine}: Always send now sends the queue without another click, until turned off`, async ({ page }, testInfo) => {
    const fixture = await queue(page, engine, mode);
    const first = row(page, "Only inspect the files.");
    const always = first.getByRole("button", { name: "Always send now", exact: true });
    expect((await always.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await always.click();
    await expect.poll(() => fixture.calls.length).toBe(1);
    expect(fixture.calls[0]).toMatchObject({ turn_id: ACTIVE, queued_turn_id: QUEUED });
    const chip = page.getByTestId("structured-always-send-now");
    await expect(chip).toBeVisible();
    expect((await chip.boundingBox())!.height).toBeGreaterThanOrEqual(testInfo.project.name === "mobile" ? 44 : 32);
    await expect(page.getByRole("textbox", { name: /^Message / })).toHaveAttribute("placeholder",
      mode === "steer" ? /sends now, into the current turn/ : /sends now, stopping the current reply/);
    await page.screenshot({ path: testInfo.outputPath("always-on.png") });
    // The first message is delivered and its turn runs: the next one follows by itself.
    const [active, queued, later] = fixture.state.turns;
    active.state = "interrupted";
    queued.state = "running";
    delete queued.delivery;
    fixture.state.active_turn = QUEUED;
    fixture.state.revision++;
    await expect.poll(() => fixture.calls.length, { timeout: 10_000 }).toBe(2);
    expect(fixture.calls[1]).toMatchObject({ turn_id: QUEUED, queued_turn_id: LATER });
    // Turned off: a message queued now waits for a click again.
    await chip.click();
    await expect(chip).toHaveAttribute("aria-pressed", "false");
    queued.state = "completed";
    later.state = "running";
    delete later.delivery;
    fixture.state.active_turn = LATER;
    fixture.state.turns.push({ ...later, turn_id: "44444444-2222-4333-8444-555555555555",
      operation_id: "44444444-2222-4333-8444-555555555555", state: "queued", text: "One more thing." });
    fixture.state.revision++;
    await expect(row(page, "One more thing.").getByRole("button", { name: "Send now", exact: true })).toBeVisible();
    await page.waitForTimeout(1_500); // two poll cycles: nothing may fire by itself
    expect(fixture.calls).toHaveLength(2);
  });
}

test("Always send now stays on across a reload, per device", async ({ page }) => {
  await queue(page, "claude-api", "interrupt");
  await row(page, "Only inspect the files.").getByRole("button", { name: "Always send now", exact: true }).click();
  await expect(page.getByTestId("structured-always-send-now")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("structured-always-send-now")).toBeVisible();
  await expect(page.getByTestId("structured-always-send-now")).toHaveAttribute("aria-pressed", "true");
  await expect(row(page, "Only inspect the files.").getByRole("button", { name: "Always send now", exact: true })).toHaveCount(0);
});

test("a refused automatic Send now is not retried until the conversation moves", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("agent-sessions.always-send-now", "1"));
  const fixture = await queue(page, "claude-api", "interrupt");
  // Another tab got there first (or the agent hasn't acknowledged its turn): the server refuses.
  await page.route(/\/send-now$/, (r) => {
    fixture.calls.push(r.request().postDataJSON());
    return r.fulfill({ status: 409, json: { detail: "a message is already waiting for this response to stop" } });
  });
  await expect.poll(() => fixture.calls.length).toBe(1);
  await page.waitForTimeout(2_500); // several poll cycles over an unchanged snapshot
  expect(fixture.calls).toHaveLength(1);
  // The click stays available as the way through.
  await expect(row(page, "Only inspect the files.").getByRole("button", { name: "Send now", exact: true })).toBeVisible();
  fixture.state.revision++; // e.g. the agent acknowledged its turn
  await expect.poll(() => fixture.calls.length).toBe(2);
  expect(fixture.calls[1]).toMatchObject({ turn_id: ACTIVE, queued_turn_id: QUEUED });
  // Bounded: a refusal that never clears stops being retried and is shown.
  for (let i = 0; i < 6; i++) {
    fixture.state.revision++;
    await page.waitForTimeout(1_200);
  }
  expect(fixture.calls.length).toBeLessThanOrEqual(5);
  await expect(page.getByTestId("structured-error")).toContainText("Send now not applied");
});

test("a message waiting for the reply to stop can still be removed", async ({ page }) => {
  const fixture = await queue(page, "claude-api", "interrupt");
  const first = row(page, "Only inspect the files.");
  await first.getByRole("button", { name: "Send now", exact: true }).click();
  await expect(first).toContainText("Waiting for the current response to stop");
  // Still queued (not sent): Remove stays, and takes it out; the stop already went out.
  await first.getByRole("button", { name: "Remove this queued message" }).click();
  await expect.poll(() => fixture.withdrawals.length).toBe(1);
  expect(fixture.withdrawals[0]).toMatchObject({ queued_turn_id: QUEUED });
  await expect(first).toHaveCount(0);
});

test("a message being steered into the turn offers no Remove", async ({ page }) => {
  await queue(page, "codex-api", "steer");
  const first = row(page, "Only inspect the files.");
  await first.getByRole("button", { name: "Send now", exact: true }).click();
  await expect(first).toContainText("Awaiting agent acknowledgement");
  await expect(first.getByRole("button", { name: "Remove this queued message" })).toHaveCount(0);
});

test("Always send now holds for the page when the browser refuses to store it", async ({ page }) => {
  // Private browsing / a full quota: storing this preference throws (other keys still work, so
  // the rest of the app loads as usual).
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === "agent-sessions.always-send-now") throw new DOMException("quota", "QuotaExceededError");
      return setItem.call(this, key, value);
    };
  });
  const fixture = await queue(page, "claude-api", "interrupt");
  await row(page, "Only inspect the files.").getByRole("button", { name: "Always send now", exact: true }).click();
  await expect.poll(() => fixture.calls.length).toBe(1);
  const chip = page.getByTestId("structured-always-send-now");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  fixture.state.revision++; // a re-read must not switch it back off
  await page.waitForTimeout(1_500);
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await chip.click();
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("aria-pressed", "false");
});
