/** Mission control directions, in a real browser (#983 P2).
 *
 * What jsdom cannot say about this: whether a fact chip lands at the caret and hands focus back to the
 * text, whether the Edit direction dialog is a bottom sheet on a phone and keeps focus inside it, what
 * a Tab press actually reaches, how big a target really is, and whether the typed text survives the
 * page byte for byte (a `<pre>` that collapsed whitespace would show the operator something other
 * than what is typed). So these assert on the REQUESTS the app sends and on what the page paints.
 * The mocks are producer-shaped; see `mission-directions.ts`.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  COPIED,
  type ConsoleServer,
  NUDGE_TEMPLATE,
  ORCH,
  PLAYBOOK_NOW,
  SAVE_REFUSAL,
  THREAD,
  TYPED,
  UNKNOWN,
  WHY,
  deferred,
  missionConsole,
  missionControlSettings,
  nudgeAction,
  objectiveRows,
  openDirection,
  playbookEditor,
  staleNudge,
} from "./mission-directions";
import { openMissionConversation, openMissionDetails } from "./mission-console";
import { setupBench } from "./terminal/harness";

/** Click a chip and wait for the caret to come back to the text, which it does on the next frame. */
async function chip(field: Locator, name: string) {
  await field.getByTestId(`objective-direction-chip-${name}`).click();
  await expect(field.getByTestId("objective-direction-text")).toBeFocused();
}

/** Wait for the ONE patch a dialog action sends, then for the dialog to go. */
async function patched(page: Page, server: ConsoleServer, n: number) {
  await expect.poll(() => server.patches.length).toBe(n);
  await expect(page.getByTestId("direction-dialog")).toHaveCount(0);
}

// --- the playbook editor ---------------------------------------------------------------------------

test("D1: a direction is written with fact chips, previewed with example facts, and saved as typed", async ({
  page,
}) => {
  const { field, text, previews, saves } = await playbookEditor(page);

  // Only what a forge_checks objective can fill, in the server's order.
  const chips = field.getByRole("group", { name: "Insert a checked fact" }).getByRole("button");
  await expect(chips).toHaveText([/^\{pr\}/, /^\{checks\}/, /^\{repo\}/, /^\{branch\}/]);
  await expect(field.getByTestId("objective-direction-chip-pr_state")).toHaveCount(0);
  await expect(field.getByTestId("objective-direction-preview")).toHaveText(
    "No direction: mission control sends your default nudge.",
  );

  await text.fill("PR #");
  await chip(field, "pr");
  await page.keyboard.type("'s checks are ");
  await chip(field, "checks");
  await page.keyboard.type(" on ");
  await chip(field, "branch");
  await page.keyboard.type(". Fix the cause and push.");
  const DIRECTION = "PR #{pr}'s checks are {checks} on {branch}. Fix the cause and push.";
  await expect(text).toHaveValue(DIRECTION);

  await expect(field.getByText("Preview · with example facts")).toBeVisible();
  await expect(field.getByTestId("objective-direction-preview")).toHaveText(
    "PR #412's checks are failure on fix/upload-retry. Fix the cause and push.",
  );
  await expect.poll(() => previews.at(-1)).toEqual({ direction: DIRECTION, probe: "forge_checks" });

  await page.getByTestId("playbook-save").click();
  await expect.poll(() => saves.length).toBe(1);
  expect(saves[0].mission_playbooks.playbooks[0].objectives[0]).toMatchObject({
    key: "checks_green",
    direction: DIRECTION,
  });
});

test("D1: an unknown placeholder shows the server's refusal in its own words, in the preview and on save", async ({
  page,
}) => {
  const { field, text, saves } = await playbookEditor(page);
  await text.fill("Fix {nope} now");
  await expect(field.getByTestId("objective-direction-preview")).toHaveText(UNKNOWN);
  await expect(field.getByTestId("objective-direction-preview")).toHaveAttribute("data-state", "error");

  await page.getByTestId("playbook-save").click();
  await expect(page.getByTestId("playbook-error")).toHaveText(SAVE_REFUSAL);
  expect(saves).toHaveLength(1);
});

test("D1: keyboard order runs from the text through each fact chip in order", async ({ page }) => {
  const { field, text } = await playbookEditor(page);
  await text.focus();
  for (const name of ["pr", "checks", "repo", "branch"]) {
    await page.keyboard.press("Tab");
    await expect(field.getByTestId(`objective-direction-chip-${name}`)).toBeFocused();
  }
  // Enter on a chip inserts it and hands the caret back to the text.
  await page.keyboard.press("Enter");
  await expect(text).toBeFocused();
  await expect(text).toHaveValue("{branch}");
});

// --- the objective menu ----------------------------------------------------------------------------

test("D2: Edit direction keeps, resets, writes and clears, and rows with a direction are marked", async ({
  page,
}) => {
  const server = await missionConsole(page);
  await openMissionDetails(page, "objectives");
  await expect(objectiveRows(page)).toHaveCount(3);
  const marks = (i: number) => objectiveRows(page).nth(i).getByTestId("objective-direction-mark");
  await expect(marks(0)).toHaveText("direction");
  await expect(marks(1)).toHaveCount(0);
  await expect(marks(2)).toHaveText("direction");

  // KEEP: the playbook's copy is the choice, it is shown, and there is nothing to save.
  let dialog = await openDirection(page, 0);
  await expect(dialog.getByTestId("direction-keep")).toBeChecked();
  await expect(dialog.getByTestId("direction-copied")).toHaveText(COPIED);
  await expect(dialog.getByTestId("direction-save")).toBeDisabled();
  await dialog.getByTestId("direction-cancel").click();
  await expect(dialog).toHaveCount(0);
  expect(server.patches).toHaveLength(0);

  // RESET: copy the playbook's CURRENT direction again.
  dialog = await openDirection(page, 0);
  await dialog.getByTestId("direction-reset").click();
  await patched(page, server, 1);
  expect(server.patches[0]).toEqual({ ops: [{ op: "reset_direction", key: "checks" }] });

  // WRITE ONE FOR THIS MISSION: starts from the current text, a chip inserts at the caret.
  dialog = await openDirection(page, 0);
  await dialog.getByTestId("direction-choice-write").click();
  const text = dialog.getByTestId("direction-field-text");
  await expect(text).toHaveValue(PLAYBOOK_NOW);
  await text.fill("PR #");
  await dialog.getByTestId("direction-field-chip-pr").click();
  await expect(text).toBeFocused();
  await page.keyboard.type(" keeps failing: fix the retry test, not the timeout.");
  const WRITTEN = "PR #{pr} keeps failing: fix the retry test, not the timeout.";
  await expect(dialog.getByTestId("direction-field-preview")).toHaveText(
    "PR #412 keeps failing: fix the retry test, not the timeout.",
  );
  await dialog.getByTestId("direction-save").click();
  await patched(page, server, 2);
  expect(server.patches[1]).toEqual({
    ops: [{ op: "set_direction", key: "checks", direction: WRITTEN }],
  });

  // A REFUSAL stays in the dialog, in the server's words, with the text kept.
  dialog = await openDirection(page, 2);
  await expect(dialog.getByTestId("direction-write")).toBeChecked();
  server.refuse = "{checks} cannot be filled for a forge_review objective";
  await dialog.getByTestId("direction-field-text").fill("Checks are {checks}");
  await dialog.getByTestId("direction-save").click();
  await expect(dialog.getByTestId("direction-dialog-error")).toHaveText(
    "{checks} cannot be filled for a forge_review objective",
  );
  await expect(dialog.getByTestId("direction-field-text")).toHaveValue("Checks are {checks}");

  // CLEAR: no direction, so a nudge types the default nudge; the row loses its mark.
  await dialog.getByTestId("direction-choice-none").click();
  await dialog.getByTestId("direction-save").click();
  await patched(page, server, 4);
  expect(server.patches[3]).toEqual({ ops: [{ op: "clear_direction", key: "review" }] });
  await expect(marks(2)).toHaveCount(0);
});

test("D2 / B6: the dialog is a sheet on a phone, holds focus in order, and Esc returns to ⋯", async ({
  page,
}, testInfo) => {
  await missionConsole(page);
  await openMissionDetails(page, "objectives");
  const dialog = await openDirection(page, 0);

  const box = await dialog.boundingBox();
  const vp = page.viewportSize()!;
  if (testInfo.project.name === "mobile") {
    expect(box!.y + box!.height).toBeGreaterThan(vp.height - 16);
    expect(box!.width).toBeGreaterThan(vp.width - 24);
  } else {
    expect(box!.width).toBeLessThanOrEqual(562);
    expect(box!.y + box!.height).toBeLessThan(vp.height - 16);
  }

  // Focus starts on the checked choice; the choices are one stop, then Reset, then the actions.
  await expect(dialog.getByTestId("direction-keep")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByTestId("direction-reset")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByTestId("direction-cancel")).toBeFocused();
  // Save is disabled with nothing to change, so the cycle stays inside the dialog.
  await page.keyboard.press("Tab");
  await expect(dialog.getByTestId("direction-keep")).toBeFocused();

  // An arrow moves the choice to Write, which opens the text and its facts in order.
  await page.keyboard.press("ArrowDown");
  await expect(dialog.getByTestId("direction-write")).toBeFocused();
  await expect(dialog.getByTestId("direction-write")).toBeChecked();
  const order = [
    "direction-field-text",
    "direction-field-chip-pr",
    "direction-field-chip-checks",
    "direction-field-chip-repo",
    "direction-field-chip-branch",
    "direction-cancel",
    "direction-save",
  ];
  for (const id of order) {
    await page.keyboard.press("Tab");
    await expect(dialog.getByTestId(id)).toBeFocused();
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("objective-menu").nth(0)).toBeFocused();
});

test("D2: a finished mission is read-only and offers no Edit direction", async ({ page }) => {
  await missionConsole(page, { state: "done" });
  await openMissionDetails(page, "objectives");
  await expect(objectiveRows(page)).toHaveCount(3);
  await expect(page.getByTestId("objective-menu")).toHaveCount(0);
  await expect(page.getByTestId("objective-edit-direction")).toHaveCount(0);
  // The mark still says which rows carry one.
  await expect(page.getByTestId("objective-direction-mark")).toHaveCount(2);
});

// --- the decision row ------------------------------------------------------------------------------

test("D3 / B1: a proposed nudge shows exactly what it will type, its facts, and the AI's why apart; Send approves", async ({
  page,
}) => {
  const server = await missionConsole(page, { pending: nudgeAction() });
  await openMissionConversation(page);
  const row = page.getByTestId("nudge-row");
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute("data-sendable", "true");
  await expect(row.getByTestId("nudge-objective")).toHaveText("Checks are green on the PR");
  await expect(row.getByText("Will type · your direction, filled")).toBeVisible();

  // BYTE FOR BYTE: the DOM text, not a whitespace-normalised match.
  expect(await row.getByTestId("nudge-text").evaluate((el) => el.textContent)).toBe(TYPED);
  await expect(row.getByTestId("nudge-fact")).toHaveText(["PR #412", "checks failure", "fix/upload-retry"]);
  await expect(row.getByTestId("nudge-provenance")).toContainText("checked at");
  await expect(row.getByTestId("nudge-provenance")).toContainText("head 4b7e0d9");
  await expect(row.getByTestId("nudge-provenance")).toContainText("not read from the session");

  const why = row.getByRole("group", { name: "Why now, written by the AI" });
  await expect(why).toContainText("Why now");
  await expect(why).toContainText("AI");
  await expect(row.getByTestId("nudge-why")).toHaveText(WHY);
  expect(await row.getByTestId("nudge-text").evaluate((el) => el.textContent)).not.toContain(WHY);

  // Keyboard: Send, then Reject.
  await row.getByTestId("nudge-send").focus();
  await page.keyboard.press("Tab");
  await expect(row.getByTestId("nudge-reject")).toBeFocused();

  await row.getByTestId("nudge-send").click();
  await expect.poll(() => server.approvals).toEqual(["act_nudge"]);
});

test("D3: the session's own pane shows the same Will type and sends through the approve route", async ({
  page,
}) => {
  const UUID = "dddddddd-1111-2222-3333-444444444444";
  const key = `claude:${UUID}`;
  await setupBench(page, {
    sessions: [{ engine: "claude", uuid: UUID, title: "Fix the flaky upload retry" }],
  });
  const approvals: string[] = [];
  let pending = [nudgeAction({ session_id: key })];
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: { config: ORCH, pending, feed: [], expired_now: 0, delivering_verbs: ["continue"] },
    }),
  );
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    approvals.push(/actions\/([^/]+)\/approve/.exec(r.request().url())![1]);
    pending = [];
    return r.fulfill({ json: { ...nudgeAction({ session_id: key }), state: "delivered" } });
  });
  await page.goto(`/s/claude/${UUID}`);
  const row = page.getByTestId("session-decisions").getByTestId("nudge-row");
  await expect(row).toBeVisible();
  expect(await row.getByTestId("nudge-text").evaluate((el) => el.textContent)).toBe(TYPED);
  await expect(row.getByTestId("nudge-objective")).toHaveText("Checks are green on the PR");
  await row.getByTestId("nudge-send").click();
  await expect.poll(() => approvals).toEqual(["act_nudge"]);
});

test("D7 / B7: a nudge whose text is no longer true says not sendable, what it was going to type, why, and offers only Dismiss", async ({
  page,
}) => {
  const server = await missionConsole(page, { pending: staleNudge() });
  await openMissionConversation(page);
  const row = page.getByTestId("nudge-row");
  await expect(row).toHaveAttribute("data-sendable", "false");
  await expect(row.getByTestId("nudge-not-sendable")).toHaveText("not sendable");
  await expect(row.getByText("Was going to type")).toBeVisible();
  expect(await row.getByTestId("nudge-text").evaluate((el) => el.textContent)).toBe(TYPED);
  await expect(row.getByTestId("nudge-stale-reason")).toHaveText(
    "The facts behind this nudge changed since it was proposed (its objective, target, head or a fact's value).",
  );
  await expect(row.getByTestId("nudge-why")).toHaveCount(0);

  await expect(row.getByRole("button")).toHaveText(["Dismiss"]);
  await expect(row.getByTestId("nudge-send")).toHaveCount(0);
  await row.getByTestId("nudge-dismiss").click();
  await expect.poll(() => server.rejects).toEqual(["act_nudge"]);
  expect(server.approvals).toEqual([]);
});

// --- the thread ------------------------------------------------------------------------------------

test("D4: a sent nudge is one row with Show text of the exact snapshot, and a held nudge is a quiet row with its reason", async ({
  page,
}) => {
  await missionConsole(page, { events: THREAD });
  await openMissionConversation(page);

  const direction = page.getByRole("group", { name: "Nudged: Checks are green on the PR" });
  await expect(direction).toContainText("Nudged");
  await expect(direction).toContainText("your direction");
  const defaultNudge = page.getByRole("group", { name: "Nudged: A PR is open for the branch" });
  await expect(defaultNudge).toContainText("your default nudge");

  const toggle = direction.getByTestId("thread-nudged-toggle");
  await expect(toggle).toHaveText("Show text");
  await expect(direction.getByTestId("thread-nudged-text")).toHaveCount(0);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toHaveText("Hide text");
  expect(
    await direction.getByTestId("thread-nudged-text").evaluate((el) => el.textContent),
  ).toBe(TYPED);

  await expect(page.getByTestId("thread-held")).toHaveCount(2);
  await expect(
    page.getByRole("group", { name: "Held: Checks are green on the PR" }).getByTestId("thread-held-reason"),
  ).toHaveText("its direction could not be filled: {pr} is missing from this objective's latest observation");
  // A held record written before stages existed reads as held too.
  await expect(
    page.getByRole("group", { name: "Held: A PR is open for the branch" }).getByTestId("thread-held-reason"),
  ).toHaveText("session is not live");
  // No meta is printed.
  await expect(page.getByTestId("thread-held").filter({ hasText: "episode" })).toHaveCount(0);
});

// --- settings --------------------------------------------------------------------------------------

test("D6: the YOLO copy says only text you wrote is typed, and the nudge field is the Default nudge", async ({
  page,
}) => {
  await missionControlSettings(page);
  const copy = page.getByTestId("orchestrator-yolo-copy");
  await expect(copy).toHaveText(
    "Even on YOLO, mission control only ever types text you wrote: an objective’s direction, filled with facts it checked itself, or your default nudge below. The AI decides when, never what. Picking an option, answering a question or starting a new session always waits for your approval.",
  );
  await expect(copy).not.toContainText(/AI-written|AI-drafted/);
  const nudge = page.getByLabel("Default nudge · sent when an objective has no direction");
  await expect(nudge).toBeVisible();
  await expect(nudge).toHaveValue(NUDGE_TEMPLATE);
  await expect(page.getByText(/Written by you, never by the\s+AI/)).toBeVisible();
  await expect(page.getByText("Nudge text", { exact: true })).toHaveCount(0);
});

// --- 44px on a phone -------------------------------------------------------------------------------

async function atLeast44(locator: Locator, label: string) {
  const n = await locator.count();
  expect(n, `${label}: nothing to measure`).toBeGreaterThan(0);
  for (let i = 0; i < n; i += 1) {
    const el = locator.nth(i);
    await el.scrollIntoViewIfNeeded();
    const b = await el.boundingBox();
    expect(b, `${label} #${i} has no box`).not.toBeNull();
    expect(b!.height, `${label} #${i} height`).toBeGreaterThanOrEqual(44);
    expect(b!.width, `${label} #${i} width`).toBeGreaterThanOrEqual(44);
  }
}

function phoneOnly(testInfo: { project: { name: string } }) {
  test.skip(testInfo.project.name !== "mobile", "the 44px floor is measured on the phone project");
}

test.describe("44px targets on a phone", () => {
  test("the editor's fact chips", async ({ page }, testInfo) => {
    phoneOnly(testInfo);
    const { field } = await playbookEditor(page);
    await atLeast44(
      field.getByRole("group", { name: "Insert a checked fact" }).getByRole("button"),
      "chip",
    );
  });

  test("the sheet's choices, Reset, chips and actions", async ({ page }, testInfo) => {
    phoneOnly(testInfo);
    await missionConsole(page);
    await openMissionDetails(page, "objectives");
    const dialog = await openDirection(page, 0);
    await dialog.getByTestId("direction-choice-write").click();
    await atLeast44(dialog.locator('[data-testid^="direction-choice-"]'), "choice");
    await atLeast44(dialog.getByTestId("direction-reset"), "reset");
    await atLeast44(dialog.locator('[data-testid^="direction-field-chip-"]'), "chip");
    await atLeast44(dialog.getByTestId("direction-cancel"), "cancel");
    await atLeast44(dialog.getByTestId("direction-save"), "save");
  });

  test("the decision row's Send and Reject, and the thread's Show text", async ({ page }, testInfo) => {
    phoneOnly(testInfo);
    await missionConsole(page, { pending: nudgeAction(), events: THREAD });
    await openMissionConversation(page);
    await atLeast44(page.getByTestId("nudge-send"), "send");
    await atLeast44(page.getByTestId("nudge-reject"), "reject");
    await atLeast44(page.getByTestId("thread-nudged-toggle"), "show text");
  });

  test("the not-sendable row's Dismiss", async ({ page }, testInfo) => {
    phoneOnly(testInfo);
    await missionConsole(page, { pending: staleNudge() });
    await openMissionConversation(page);
    await atLeast44(page.getByTestId("nudge-dismiss"), "dismiss");
  });
});

// --- a save that is still pending (#997 review 4880) --------------------------------------------------

test("a pending Save freezes the text and its fact chips, so nothing typed while it is pending is lost", async ({
  page,
}) => {
  const server = await missionConsole(page);
  await openMissionDetails(page, "objectives");
  const dialog = await openDirection(page, 0);
  await dialog.getByTestId("direction-choice-write").click();
  const text = dialog.getByTestId("direction-field-text");
  const A = "PR #{pr}: fix the retry test, not the timeout.";
  await text.fill(A);

  const gate = deferred();
  server.hold = gate.promise;
  await dialog.getByTestId("direction-save").click();
  await expect.poll(() => server.patches.length).toBe(1);

  // The request is in flight: what is on screen is what was sent, and it cannot be changed under it.
  await expect(text).toBeDisabled();
  const chips = dialog.locator('[data-testid^="direction-field-chip-"]');
  await expect(chips).not.toHaveCount(0);
  for (const c of await chips.all()) await expect(c).toBeDisabled();
  // Type anyway, the way the review did: nothing reaches the text.
  await text.click({ force: true });
  await page.keyboard.type(" And this.");
  await expect(text).toHaveValue(A);

  gate.release();
  await expect(dialog).toHaveCount(0);
  expect(server.patches).toEqual([{ ops: [{ op: "set_direction", key: "checks", direction: A }] }]);
});

for (const kind of ["reset", "clear"] as const) {
  test(`a pending ${kind} keeps Tab and Shift+Tab inside the dialog, with the background inert`, async ({
    page,
  }) => {
    const server = await missionConsole(page);
    await openMissionDetails(page, "objectives");
    const dialog = await openDirection(page, 0);

    const gate = deferred();
    server.hold = gate.promise;
    if (kind === "reset") {
      await dialog.getByTestId("direction-reset").click();
    } else {
      await dialog.getByTestId("direction-choice-none").click();
      await dialog.getByTestId("direction-save").click();
    }
    await expect.poll(() => server.patches.length).toBe(1);
    // Every control in the dialog is disabled while it is pending — the state the trap had no answer for.
    await expect(dialog.getByTestId("direction-cancel")).toBeDisabled();
    await expect(dialog.getByTestId("direction-save")).toBeDisabled();

    const focusInDialog = () =>
      page.evaluate(() => !!document.activeElement?.closest('[data-testid="direction-dialog"]'));
    for (const key of ["Tab", "Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"]) {
      await page.keyboard.press(key);
      expect(await focusInDialog(), `focus left the dialog on ${key}`).toBe(true);
    }
    // …and nothing behind it can be reached while it is open.
    expect(await page.evaluate(() => document.getElementById("root")?.inert)).toBe(true);

    gate.release();
    await expect(dialog).toHaveCount(0);
    expect(server.patches).toEqual([
      { ops: [{ op: kind === "reset" ? "reset_direction" : "clear_direction", key: "checks" }] },
    ]);
    expect(await page.evaluate(() => document.getElementById("root")?.inert)).toBe(false);
  });
}
