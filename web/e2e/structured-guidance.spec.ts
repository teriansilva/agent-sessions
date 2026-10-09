import { expect, test, type Page } from "@playwright/test";
import { ACTIVE, COMMAND, ID, QUEUED, clients, mockStructuredGuidance } from "./structuredGuidanceFixture";

async function conversation(page: Page, engine: string, mode: string, theme = "dark") {
  const fixture = await mockStructuredGuidance(page, engine, mode, theme);
  await page.goto(`/s/${engine}/${ID}`);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  return fixture;
}

for (const [engine, mode] of clients) {
  for (const theme of ["dark", "light"]) {
    test(`${engine}: compact activity and readable reply (${theme})`, async ({ page }, testInfo) => {
      await conversation(page, engine, mode, theme);
      const activity = page.getByTestId("structured-activity");
      await expect(activity).toBeVisible();
      await expect(activity.locator("summary")).toContainText("12 commands & tools");
      await expect(page.getByText(COMMAND, { exact: true })).toBeHidden();
      await expect(page.getByTestId("structured-reply")).toContainText("parallel session startup");
      await page.screenshot({ path: testInfo.outputPath("collapsed.png") });
      await activity.locator("summary").focus();
      await page.keyboard.press("Enter");
      await expect(page.getByText(COMMAND, { exact: true })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("expanded.png") });
      await page.keyboard.press("Enter");
      await expect(page.getByText(COMMAND, { exact: true })).toBeHidden();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
    });
  }
  test(`${engine}: Send now targets the original queued and active messages`, async ({ page }) => {
    const { state, calls } = await conversation(page, engine, mode);
    const queued = page.getByTestId("structured-turn").filter({ hasText: "Only inspect the files." });
    await expect(queued).toContainText(mode === "steer" ? "Adds this message to the current turn" : "Interrupts the current response");
    const sendNow = queued.getByRole("button", { name: "Send now", exact: true });
    expect((await sendNow.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await sendNow.click();
    await expect.poll(() => calls.length).toBe(1);
    expect(calls[0]).toMatchObject({ turn_id: ACTIVE, queued_turn_id: QUEUED });
    expect(calls[0].operation_id).toMatch(/^[0-9a-f-]{36}$/);
    await expect(queued).toContainText(mode === "steer" ? "Awaiting agent acknowledgement" : "Waiting for the current response to stop");
    expect(state.turns[1].text).toBe("Only inspect the files.");
    await expect(sendNow).toBeHidden();
  });
  for (const recovery of ["accepted handoff", "IPC response lost", "worker stopped", "turn omitted"]) {
    test(`${engine}: lost response recovery survives ${recovery}`, async ({ page }, testInfo) => {
      const fixture = await conversation(page, engine, mode);
      // Accepted IPC failures must be 503, not a definite 409; pinned by real-worker tests.
      fixture.fail(true, recovery === "IPC response lost" ? 503 : undefined);
      await page.getByRole("button", { name: "Send now", exact: true }).click();
      const retry = page.getByRole("button", { name: "Retry Send now", exact: true });
      await expect(retry).toBeVisible();
      await expect(page.getByText(mode === "steer" ? "Awaiting agent acknowledgement"
        : "Waiting for the current response to stop · sends next", { exact: true })).toBeVisible();
      if (recovery === "worker stopped" || recovery === "turn omitted") {
        fixture.state.active_turn = "";
        fixture.state.native.send_now = "";
        fixture.state.native.worker = "";
        fixture.state.state = "unavailable";
        fixture.state.turns.forEach((t) => { t.state = "uncertain"; });
        if (recovery === "turn omitted") {
          fixture.state.turns = [];
          fixture.state.omitted_turns = 2;
        }
        fixture.state.revision++;
        if (recovery === "turn omitted") await expect(page.getByText("2 earlier turns are not shown.")).toBeVisible();
        else await expect(page.getByTestId("structured-turn").first()).toHaveAttribute("data-state", "uncertain");
      }
      await expect(retry).toBeEnabled();
      await page.screenshot({ path: testInfo.outputPath("recovery.png") });
      // A durable replay observes the original receipt without changing the later snapshot.
      await page.route("**/send-now", async (r) => {
        fixture.calls.push(r.request().postDataJSON());
        await r.fulfill({ json: { handoff: "sent", operation_id: fixture.calls[0].operation_id } });
      });
      await retry.click();
      await expect.poll(() => fixture.calls.length).toBe(2);
      expect(fixture.calls[1]).toEqual(fixture.calls[0]);
      expect(fixture.calls[1]).toMatchObject({ turn_id: ACTIVE, queued_turn_id: QUEUED });
      await expect(retry).toBeHidden();
    });
  }
  test(`${engine}: a refused retry cannot erase an earlier uncertain request`, async ({ page }) => {
    const fixture = await conversation(page, engine, mode);
    fixture.fail(true, 503);
    await page.getByRole("button", { name: "Send now", exact: true }).click();
    const retry = page.getByRole("button", { name: "Retry Send now", exact: true });
    await expect(retry).toBeEnabled();
    let refuseRetry = true;
    await page.route("**/send-now", async (r) => {
      fixture.calls.push(r.request().postDataJSON());
      await r.fulfill(refuseRetry
        ? { status: 409, json: { detail: "the worker cannot be reached for this retry" } }
        : { json: { handoff: "sent", operation_id: fixture.calls[0].operation_id } });
    });
    await retry.click();
    await expect.poll(() => fixture.calls.length).toBe(2);
    await expect(retry).toBeEnabled();
    refuseRetry = false;
    await retry.click();
    await expect.poll(() => fixture.calls.length).toBe(3);
    expect(fixture.calls[1]).toEqual(fixture.calls[0]);
    expect(fixture.calls[2]).toEqual(fixture.calls[0]);
    await expect(retry).toBeHidden();
  });
  test(`${engine}: a proven pre-write refusal permits a new deliberate selection`, async ({ page }) => {
    const fixture = await conversation(page, engine, mode);
    fixture.fail(false, 409);
    const sendNow = page.getByRole("button", { name: "Send now", exact: true });
    await sendNow.click();
    await expect(page.getByTestId("structured-error")).toContainText("Send now not applied");
    await expect(page.getByRole("button", { name: "Retry Send now", exact: true })).toBeHidden();
    await expect(sendNow).toBeEnabled();
    fixture.recover();
    await sendNow.click();
    await expect.poll(() => fixture.calls.length).toBe(2);
    expect(fixture.calls[1].operation_id).not.toBe(fixture.calls[0].operation_id);
    expect(fixture.calls[1]).toMatchObject({ turn_id: ACTIVE, queued_turn_id: QUEUED });
  });
}

test("uncertain Send now retries the same selection after the active turn changes", async ({ page }) => {
  const fixture = await conversation(page, "claude-api", "interrupt");
  fixture.fail();
  await page.getByRole("button", { name: "Send now", exact: true }).click();
  await expect(page.getByRole("button", { name: "Retry Send now", exact: true })).toBeVisible();
  // A later snapshot must not silently retarget a retry to this new response.
  fixture.state.active_turn = "33333333-2222-4333-8444-555555555555";
  fixture.state.turns[0].state = "completed";
  fixture.state.turns.push({ ...fixture.state.turns[0], turn_id: fixture.state.active_turn,
    operation_id: fixture.state.active_turn, state: "running", text: "A newer response is active", tools: [] });
  fixture.state.revision++;
  await expect(page.getByText("A newer response is active", { exact: true })).toBeVisible();
  fixture.recover();
  await page.getByRole("button", { name: "Retry Send now", exact: true }).click();
  await expect.poll(() => fixture.calls.length).toBe(2);
  expect(fixture.calls[1]).toEqual(fixture.calls[0]);
  expect(fixture.calls[0].turn_id).toBe(ACTIVE);
});

test("approval stays visible outside collapsed risky activity; read-only and old workers cannot Send now", async ({ page }, testInfo) => {
  const { state } = await conversation(page, "opencode-api", "interrupt");
  state.turns[0].state = "awaiting_approval";
  state.turns[0].tools = [{ id: "risky", name: "command", summary: "git push --force", outcome: "failed",
    risk: { level: "risky", reasons: ["rewrites remote history"] } }];
  state.pending_requests = [{ request_id: "req", turn_id: ACTIVE, kind: "command", complete: true,
    choices: ["approve", "reject"], payload: { command: "git status --short", cwd: "/home/u/demo" } }];
  state.revision++;
  await expect(page.getByRole("button", { name: "Approve once", exact: true })).toBeVisible();
  await expect(page.getByTestId("structured-activity").locator("summary")).toContainText("Risky activity");
  await expect(page.getByText("git push --force", { exact: true })).toBeHidden();
  await expect(page.getByText("git status --short", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve once", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("approval-visible.png") });
  state.native.send_now = "";
  state.revision++;
  await expect(page.getByRole("button", { name: "Send now", exact: true })).toBeHidden();
  state.native.send_now = "interrupt";
  state.read_only = "agent removed";
  state.revision++;
  await expect(page.getByTestId("structured-pane")).toContainText("agent removed");
  await expect(page.getByRole("button", { name: "Send now", exact: true })).toBeHidden();
});
