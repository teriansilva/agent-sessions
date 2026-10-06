/** Missions → Automations (#1201 Phase 1, PR B) in a real browser, on desktop and phone.
 *
 *  The flows are the phase's acceptance: create, enable WITH CONSENT, Run now, and see the run with
 *  its dispatch facts. Around them, the things only a browser can judge: the consent dialog is a
 *  real modal whose Confirm needs the tick and resends the digest it SHOWED; a widening names what
 *  widened; the kill switch reads as a warning; targets hold 44px and nothing overflows at 320 and
 *  412; the editor can be driven by keyboard alone; and an automation's session wears its badge in
 *  the sidebar. The API is a stateful fake (`./automations.ts`); what the page SENT is asserted from
 *  its recorded requests.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  AUTOMATION_NEW_PATH,
  AUTOMATIONS_PATH,
  automationEditPath,
  automationPath,
} from "../src/lib/routes";
import { automation, mockAutomations } from "./automations";

const SESSION_ACTION = {
  kind: "start_session",
  engine: "claude",
  model: null,
  folder: "/repo",
  bypass: false,
  message: { text: "Review the open PRs" },
};

function lastRequest(fake: { requests: { method: string; path: string; body: unknown }[] }, method: string, path: RegExp) {
  return [...fake.requests].reverse().find((r) => r.method === method && path.test(r.path));
}

test("create a schedule automation, enable it with consent, and the row reads ENABLED", async ({ page }) => {
  const fake = await mockAutomations(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("automation-name").fill("Nightly dependency audit");
  // Schedule · daily · 03:00 is the default; the action is a mission.
  await expect(page.getByTestId("trigger-kind").locator('[aria-pressed="true"]')).toHaveText("Schedule");
  await page.getByTestId("mission-project").selectOption("p1");
  await page.getByTestId("message-text").fill("Audit the dependencies");
  await page.getByTestId("editor-save").click();

  await expect(page).toHaveURL(new RegExp(`${automationEditPath("new1")}$`));
  await expect(page.getByTestId("editor-notice")).toContainText("off until you enable it");
  const created = lastRequest(fake, "POST", /^$/)!.body as Record<string, unknown>;
  expect(created.trigger).toEqual({
    kind: "schedule",
    cadence: { kind: "daily", time: "03:00" },
    tz: "UTC",
  });
  expect(created.action).toMatchObject({ kind: "start_mission", project_id: "p1", autonomy: "propose" });

  await page.getByTestId("editor-enable").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("consent-scope")).toContainText("Starts a mission in project p1");
  const confirm = dialog.getByTestId("consent-confirm");
  await expect(confirm).toBeDisabled();
  await dialog.getByTestId("consent-agree").check();
  await confirm.click();
  await expect(dialog).toBeHidden();
  expect(lastRequest(fake, "POST", /\/enable$/)!.body).toEqual({
    revision: 1,
    consent: true,
    scope_digest: "digest-new1-1",
  });

  await page.goto(AUTOMATIONS_PATH);
  const row = page.locator('[data-testid="automation-row"][data-id="new1"]');
  await expect(row.getByTestId("automation-state")).toHaveText("Enabled");
});

test("a widening save names what widened and resends with the digest it showed", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [automation()],
    widenOnPatch: ["higher mission autonomy"],
  });
  await page.goto(automationEditPath("a1"));
  await page.getByTestId("mission-autonomy").selectOption("dispatch");
  await page.getByTestId("editor-save").click();

  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog).toBeVisible();
  // It is already enabled: the question is saving changes, never "re-enabling" it.
  await expect(dialog.getByRole("heading")).toHaveText("Save changes to “Nightly dependency audit”?");
  await expect(dialog.locator("[data-widened]")).toHaveCount(1);
  await expect(dialog.locator("[data-widened]")).toContainText(
    "Autonomy: dispatches the plan without asking you",
  );
  await expect(dialog.locator("[data-widened]")).toContainText("Widened");
  await expect(dialog.getByTestId("consent-widened")).toContainText("higher mission autonomy");
  // The first PATCH carried no consent — the server decided it widened.
  expect(lastRequest(fake, "PATCH", /^\/a1$/)!.body).not.toHaveProperty("consent");

  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toBeHidden();
  expect(lastRequest(fake, "PATCH", /^\/a1$/)!.body).toMatchObject({
    revision: 1,
    consent: true,
    scope_digest: "digest-a1-2",
  });
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  await expect(page.getByTestId("editor-scope-lines")).toContainText(
    "Autonomy: dispatches the plan without asking you",
  );
});

test("Run now starts a run, and the run shows its dispatch facts", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation({ name: "PR review", action: SESSION_ACTION })],
  });
  await page.goto(AUTOMATIONS_PATH);
  const row = page.getByTestId("automation-row").first();
  await row.getByTestId("automation-run-now").click();
  const notice = page.getByTestId("automations-notice");
  await expect(notice).toContainText("“PR review” is running.");
  await notice.getByRole("link", { name: "View the run" }).click();

  await expect(page).toHaveURL(/\/mission\/automations\/a1\?run=run1$/);
  const detail = page.getByTestId("run-detail");
  await expect(detail.getByTestId("run-outcome")).toHaveText("OK");
  await expect(detail.getByTestId("run-steps").locator("li")).toHaveText([
    /Slot claimed/,
    /Launched/,
    /Started/,
    /Briefed/,
    /Done/,
  ]);
  await expect(detail.getByRole("link", { name: "Open session" })).toHaveAttribute(
    "href",
    "/s/claude/22222222-2222-4222-8222-222222222222",
  );
  await expect(page.getByTestId("run-row")).toHaveCount(1);
});

test("Run now on an automation the server has turned off says why it was refused", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation()],
    runRefusal: "this automation is turned off; enable it first",
  });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("automation-run-now").first().click();
  await expect(page.getByTestId("automations-notice")).toContainText(
    "Run now was refused: this automation is turned off; enable it first",
  );
});

test("the kill switch is a warning, not a failure, and Run now waits for it", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()], loopEnabled: false });
  await page.goto(AUTOMATIONS_PATH);
  const notice = page.getByTestId("automations-kill-switch");
  await expect(notice).toContainText("Automations are switched off on this server");
  const [edge, degraded, down] = await notice.evaluate((el) => {
    const probe = (v: string) => {
      const p = document.createElement("i");
      p.style.color = `var(${v})`;
      document.body.append(p);
      const c = getComputedStyle(p).color;
      p.remove();
      return c;
    };
    return [getComputedStyle(el).borderLeftColor, probe("--status-degraded"), probe("--status-down")];
  });
  expect(edge).toBe(degraded);
  expect(edge).not.toBe(down);
  await expect(page.getByTestId("automation-run-now").first()).toBeDisabled();
});

async function targetsAndOverflow(page: Page, root: string) {
  const report = await page.evaluate((sel) => {
    const scope = document.querySelector(sel)!;
    const small: string[] = [];
    for (const el of scope.querySelectorAll<HTMLElement>(
      "button, select, input:not([type=checkbox]), a",
    )) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
      // An inline link inside a sentence is exempt (WCAG 2.5.8); every other control is not.
      if (el.tagName === "A" && el.closest("p, [role=status], [role=alert]")) continue;
      const tooNarrow = el.tagName === "BUTTON" && r.width < 44;
      if (r.height < 44 || tooNarrow)
        small.push(`${el.tagName} "${(el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 30)}" ${Math.round(r.width)}×${Math.round(r.height)}`);
    }
    const doc = document.scrollingElement!;
    const page = scope as HTMLElement;
    return {
      small,
      docOverflow: doc.scrollWidth - doc.clientWidth,
      pageOverflow: page.scrollWidth - page.clientWidth,
    };
  }, root);
  expect(report.small, "controls under 44px").toEqual([]);
  expect(report.docOverflow, "document overflows horizontally").toBeLessThanOrEqual(0);
  expect(report.pageOverflow, "the page overflows horizontally").toBeLessThanOrEqual(0);
}

for (const width of [320, 412]) {
  test(`44px targets and no horizontal overflow at ${width}px — list, editor, runs`, async ({ page }) => {
    await page.setViewportSize({ width, height: 860 });
    await mockAutomations(page, {
      automations: [
        automation(),
        automation({
          id: "a2",
          name: "Morning stand-up summary with a long name that must wrap",
          state: "paused",
          paused: true,
          paused_reason: "paused after 3 failed runs in a row",
          next_run: null,
        }),
      ],
    });
    await page.goto(AUTOMATIONS_PATH);
    await expect(page.getByTestId("automation-row")).toHaveCount(2);
    await expect(page.getByTestId("why-not-running")).toBeVisible();
    await targetsAndOverflow(page, '[data-testid="automations-page"]');

    await page.goto(automationEditPath("a1"));
    await expect(page.getByTestId("automation-editor")).toBeVisible();
    await targetsAndOverflow(page, '[data-testid="automation-editor"]');
    // The session action's fields too: agent, model, folder, bypass.
    await page.getByTestId("action-kind").getByRole("button", { name: "Start a session" }).click();
    await expect(page.getByTestId("session-engine")).toBeVisible();
    await targetsAndOverflow(page, '[data-testid="automation-editor"]');

    await page.goto(automationPath("a1"));
    await expect(page.getByTestId("runs-empty")).toBeVisible();
    await targetsAndOverflow(page, '[data-testid="automation-runs"]');
  });
}

/** Tab until `testId` has focus. Keyboard only: nothing here clicks. */
async function tabTo(page: Page, testId: string, max = 200) {
  for (let i = 0; i < max; i++) {
    const at = await page.evaluate(() => document.activeElement?.getAttribute("data-testid") ?? "");
    if (at === testId) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`never reached ${testId} by Tab`);
}

test("the editor can be filled and saved with the keyboard alone", async ({ page }) => {
  const fake = await mockAutomations(page);
  await page.goto(AUTOMATIONS_PATH);
  await tabTo(page, "automation-new");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${AUTOMATION_NEW_PATH}$`));
  await expect(page.getByTestId("automation-name")).toBeVisible();

  await tabTo(page, "automation-name");
  await page.keyboard.type("Keyboard audit");
  // Once: move to the trigger group and press its first button.
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveText("Once");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus")).toHaveText("Run now only");
  await page.keyboard.press("Space");
  await expect(page.getByTestId("trigger-kind").locator('[aria-pressed="true"]')).toHaveText(
    "Run now only",
  );
  await tabTo(page, "mission-project");
  await page.keyboard.press("ArrowDown");
  await expect(page.getByTestId("mission-project")).toHaveValue("p1");
  await tabTo(page, "message-text");
  await page.keyboard.type("Check the lockfile");
  await tabTo(page, "editor-save");
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(new RegExp(`${automationEditPath("new1")}$`));
  expect(lastRequest(fake, "POST", /^$/)!.body).toMatchObject({
    name: "Keyboard audit",
    trigger: { kind: "manual" },
    action: { kind: "start_mission", project_id: "p1", instruction: { text: "Check the lockfile" } },
  });
  // And the consent dialog is reachable and operable the same way.
  await tabTo(page, "editor-enable");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("consent-dialog")).toBeVisible();
  await expect(page.getByTestId("consent-agree")).toBeFocused();
  await page.keyboard.press("Space");
  await tabTo(page, "consent-confirm");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("consent-dialog")).toBeHidden();
  expect(lastRequest(fake, "POST", /\/enable$/)!.body).toMatchObject({ consent: true });
});

test("a session an automation started wears its badge in the sidebar", async ({ page }) => {
  const id = "claude:11111111-1111-4111-8111-111111111111";
  await mockAutomations(page, {
    sessions: [
      {
        id,
        engine: "claude",
        uuid: id.slice(7),
        short_uuid: "11111111",
        cwd: "/repo",
        project: { kind: "folder", id: "/repo", name: "/repo" },
        last_mtime: 1_700_000_000,
        first_user_message: "review",
        title: "Nightly review session",
        sticky: false,
        archived: false,
      },
    ],
    origins: {
      [id]: {
        kind: "session",
        automation_id: "a1",
        name: "Nightly dependency audit",
        run_id: "run1",
        deleted: false,
      },
    },
  });
  await page.goto("/");
  const open = page.getByRole("button", { name: /Open (session|mission) list/i });
  if (await open.count()) await open.first().click();
  const row = page.locator("ul[aria-label$='sessions'] li").filter({ hasText: "Nightly review session" });
  await expect(row.getByTestId("origin-badge")).toHaveText(/Auto · Nightly dependency audit/);
  await expect(row.getByTestId("origin-badge")).toHaveAttribute(
    "title",
    "Started by the automation “Nightly dependency audit”",
  );
});

test("an empty list says so and offers the first automation", async ({ page }) => {
  await mockAutomations(page);
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-empty")).toContainText("No automations yet");
  await expect(page.getByTestId("automations-empty").getByRole("link", { name: /New automation/ })).toBeVisible();
});

test("Library names Automations beside Templates, Checklists and Playbooks (#1294)", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()] });
  await page.goto(AUTOMATIONS_PATH);
  if ((await page.locator(".app.navOpen").count()) > 0) await page.keyboard.press("Escape");
  await page.locator('.hud-topbar [data-testid="section-menu-library"]').click();
  const menu = page.locator('[data-testid="section-menu-library-panel"]');
  await expect(menu.locator("a[data-subsection]")).toHaveText(["Templates", "Automations", "Checklists", "Playbooks"]);
  await expect(menu.locator('a[data-subsection="automations"]')).toHaveAttribute("aria-current", "page");
});

function pastRun(id: string, outcome: string, reason: string, ago: number, over: Record<string, unknown> = {}) {
  const t = Math.floor(Date.now() / 1000) - ago;
  return {
    id,
    automation_id: "a1",
    trigger: "schedule",
    slot: "2026-09-27T03:00",
    fire_at: t,
    catch_up: false,
    covered: 1,
    state: "done",
    outcome,
    result_class: outcome === "ok" ? "ok" : outcome === "skipped" ? "skipped" : "failed",
    reason,
    mission_id: null,
    session_key: null,
    created_at: t,
    finished_at: t,
    inputs: {},
    scope: {},
    steps: [{ seq: 1, at: t, step: outcome, detail: reason }],
    ...over,
  };
}

test("run history lists the runs that did not run, each with its reason", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation()],
    runs: {
      a1: [
        pastRun("r4", "interrupted", "interrupted: outcome unknown — the app stopped while this run was starting", 3600),
        pastRun("r3", "refused", "that folder is outside your project folders", 2 * 86400),
        pastRun("r2", "skipped", "skipped: previous run still in its effect", 3 * 86400),
        pastRun("r2b", "stopped", "stopped: you paused it during the run", 3 * 86400 + 60),
        pastRun("r1", "ok", "mission done", 4 * 86400, { catch_up: true, covered: 2 }),
      ],
    },
  });
  await page.goto(automationPath("a1"));
  const rows = page.getByTestId("run-row");
  await expect(rows).toHaveCount(5);
  await expect(rows.nth(0)).toContainText("Interrupted");
  await expect(rows.nth(1)).toContainText("Refused");
  await expect(rows.nth(1)).toContainText("outside your project folders");
  await expect(rows.nth(2)).toContainText("Skipped");
  await expect(rows.nth(2)).toContainText("previous run still in its effect");
  await expect(rows.nth(3)).toContainText("Stopped");
  await expect(rows.nth(4)).toContainText("catch-up · covered 2 missed slots");
  // The newest run is open, and an interrupted one says its outcome is unknown.
  await expect(page.getByTestId("run-detail")).toContainText("Outcome unknown");
  await rows.nth(1).click();
  await expect(page).toHaveURL(/\?run=r3$/);
  await expect(page.getByTestId("run-detail").getByTestId("run-outcome")).toHaveText("Refused");
});

test("a failed reload keeps the automations it already shows, and says when they were loaded", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()], listFailAfterFirst: 503 });
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("automation-row")).toHaveCount(1);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  const err = page.getByTestId("automations-load-error");
  await expect(err).toContainText("Couldn’t load automations");
  await expect(err).toContainText("Showing what was loaded at");
  await expect(page.getByTestId("automation-row")).toHaveCount(1);
});


const colourOf = (page: Page, token: string) =>
  page.evaluate((v) => {
    const p = document.createElement("i");
    p.style.backgroundColor = `var(${v})`;
    document.body.append(p);
    const c = getComputedStyle(p).backgroundColor;
    p.remove();
    return c;
  }, token);

test("the strip: a never-run automation has fourteen empty days, a failed day is red", async ({ page }) => {
  await mockAutomations(page, {
    automations: [
      automation({
        id: "never",
        name: "Never ran",
        state: "off",
        enabled: false,
        consented_at: null,
        next_run: null,
        stats: { ok: 0, failed: 0, skipped: 0, pending: 0, runs: 0, success_rate: null },
      }),
      automation({
        id: "failing",
        name: "Failing",
        stats: { ok: 10, failed: 3, skipped: 0, pending: 0, runs: 13, success_rate: 0.77 },
      }),
    ],
  });
  await page.goto(AUTOMATIONS_PATH);
  const never = page.locator('[data-testid="automation-row"][data-id="never"]');
  await expect(never.locator("[data-worst]")).toHaveCount(14);
  await expect(never.locator('[data-worst="none"]')).toHaveCount(14);
  await expect(never.getByTestId("strip-words")).toHaveText("no runs");
  const empty = await never
    .locator('[data-worst="none"]')
    .evaluateAll((els) => els.map((e) => getComputedStyle(e).backgroundColor));
  expect(new Set(empty)).toEqual(new Set(["rgba(0, 0, 0, 0)"]));

  const failing = page.locator('[data-testid="automation-row"][data-id="failing"]');
  await expect(failing.locator('[data-worst="failed"]')).toHaveCount(3);
  const red = await failing
    .locator('[data-worst="failed"]')
    .first()
    .evaluate((e) => getComputedStyle(e).backgroundColor);
  expect(red).toBe(await colourOf(page, "--status-down"));
  const green = await failing
    .locator('[data-worst="ok"]')
    .first()
    .evaluate((e) => getComputedStyle(e).backgroundColor);
  expect(green).toBe(await colourOf(page, "--status-up"));
});

test("an enable whose scope moved re-opens on the new lines, unticked, and sends the NEW digest", async ({ page }) => {
  const moved = [...automation().scope_lines as string[], "Checklist: audit"];
  const fake = await mockAutomations(page, {
    automations: [
      automation({ state: "off", enabled: false, consented_at: null, next_run: null }),
    ],
    enableScopeMovedOnce: moved,
  });
  await page.goto(automationPath("a1"));
  await page.getByTestId("runs-enable").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog.getByTestId("consent-scope")).not.toContainText("Checklist: audit");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();

  await expect(dialog.getByTestId("consent-scope")).toContainText("Checklist: audit");
  await expect(dialog).toContainText("The scope changed again since you opened this");
  await expect(dialog.getByTestId("consent-agree")).not.toBeChecked();
  await expect(dialog.getByTestId("consent-confirm")).toBeDisabled();
  expect(lastRequest(fake, "POST", /\/enable$/)!.body).toMatchObject({ scope_digest: "digest-1" });

  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toBeHidden();
  expect(lastRequest(fake, "POST", /\/enable$/)!.body).toEqual({
    revision: 1,
    consent: true,
    scope_digest: "digest-1-moved",
  });
});

test("a save whose scope moved keeps naming what widened, and resends the new digest", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [automation()],
    widenOnPatch: ["higher mission autonomy"],
    patchScopeMovedOnce: true,
  });
  await page.goto(automationEditPath("a1"));
  await page.getByTestId("mission-autonomy").selectOption("dispatch");
  await page.getByTestId("editor-save").click();
  const dialog = page.getByTestId("consent-dialog");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();

  // The 409 carries no `widened`; the dialog still names it, and says the scope moved.
  await expect(dialog.getByTestId("consent-scope")).toContainText("Checklist: audit");
  await expect(dialog).toContainText("The scope changed again since you opened this");
  await expect(dialog.getByTestId("consent-widened")).toContainText("higher mission autonomy");
  await expect(dialog.locator("[data-widened]")).toContainText("Autonomy: dispatches the plan");
  await expect(dialog.getByTestId("consent-agree")).not.toBeChecked();
  await expect(dialog.getByTestId("consent-confirm")).toBeDisabled();

  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toBeHidden();
  expect(lastRequest(fake, "PATCH", /^\/a1$/)!.body).toMatchObject({
    consent: true,
    scope_digest: "digest-a1-2-moved",
  });
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
});

test("re-approval shows WHY it needs approval inside the dialog", async ({ page }) => {
  await mockAutomations(page, {
    automations: [
      automation({
        state: "needs_reapproval",
        needs_reapproval: true,
        paused: true,
        reapproval_reason: "the template was edited since you approved this automation",
        next_run: null,
      }),
    ],
  });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("why-not-running").getByRole("button", { name: "Review and approve" }).click();
  await expect(page.getByTestId("consent-reason")).toContainText(
    "the template was edited since you approved this automation",
  );
});

test("an enable on a stale revision reads the automation again instead of retrying it", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [automation({ state: "off", enabled: false, consented_at: null, next_run: null })],
    enableStaleOnce: true,
  });
  await page.goto(automationPath("a1"));
  await page.getByTestId("runs-enable").click();
  const dialog = page.getByTestId("consent-dialog");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toContainText("It changed since you opened this");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toBeHidden();
  expect(lastRequest(fake, "POST", /\/enable$/)!.body).toMatchObject({ revision: 2, consent: true });
});

test("when the server names what widened on a moved scope, its list wins", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation()],
    widenOnPatch: ["higher mission autonomy"],
    patchScopeMovedOnce: true,
    patchScopeMovedWidened: ["higher mission autonomy", "the checklist changed"],
  });
  await page.goto(automationEditPath("a1"));
  await page.getByTestId("mission-autonomy").selectOption("dispatch");
  await page.getByTestId("editor-save").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog.getByTestId("consent-widened")).not.toContainText("the checklist changed");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog.getByTestId("consent-widened")).toContainText("the checklist changed");
  await expect(dialog.locator('[data-widened]:has-text("Checklist: audit")')).toHaveCount(1);
});

test("a change acknowledged while a run is in flight says so, in amber, beside the confirmation", async ({ page }) => {
  await mockAutomations(page, { automations: [automation()], inFlight: true });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("automation-more").first().click();
  await page.getByTestId("automation-menu").getByRole("menuitem", { name: "Pause" }).click();
  const notice = page.getByTestId("automations-notice");
  await expect(notice).toContainText("paused");
  const note = notice.getByTestId("in-flight-note");
  await expect(note).toHaveText("Note: a run already in progress may still complete.");
  const [ink, warn] = await note.evaluate((el) => {
    const p = document.createElement("i");
    p.style.color = "var(--warn-text)";
    document.body.append(p);
    const c = getComputedStyle(p).color;
    p.remove();
    return [getComputedStyle(el).color, c];
  });
  expect(ink).toBe(warn);
  // Not an error: the confirmation itself is the ok notice.
  await expect(notice).not.toHaveAttribute("role", "alert");
});

test("a run that typed but did not submit is shown prominently, with its session", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation({ action: SESSION_ACTION })],
    runs: {
      a1: [
        pastRun("r1", "partial", "text was typed but not submitted — check the terminal", 600, {
          session_key: "claude:33333333-3333-4333-8333-333333333333",
        }),
      ],
    },
  });
  await page.goto(automationPath("a1"));
  const partial = page.getByTestId("run-partial");
  await expect(partial).toContainText("text was typed but not submitted — check the terminal");
  await expect(partial.getByRole("link", { name: "Open session" })).toHaveAttribute(
    "href",
    "/s/claude/33333333-3333-4333-8333-333333333333",
  );
  await expect(page.getByTestId("run-row").first()).toContainText("Typed, not sent");
});

test("inputs that can't be checked right now are said, never shown as a scope to approve", async ({ page }) => {
  await mockAutomations(page, {
    automations: [
      automation({
        state: "off",
        enabled: false,
        consented_at: null,
        next_run: null,
        scope: null,
        scope_lines: [],
        scope_digest: null,
        check_note: "not checked: the template store could not be read",
      }),
    ],
  });
  await page.goto(AUTOMATIONS_PATH);
  await expect(page.getByTestId("why-not-running")).toContainText(
    "Not checked: the template store could not be read.",
  );
  await page.goto(automationPath("a1"));
  await page.getByTestId("runs-enable").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog.getByTestId("consent-scope")).toHaveText("Its inputs can’t be checked right now.");
  await expect(dialog.getByTestId("consent-agree")).toBeDisabled();
  await expect(dialog.getByTestId("consent-confirm")).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.goto(automationEditPath("a1"));
  await expect(page.getByTestId("editor-scope-unavailable")).toBeVisible();
});

test("an unknown automation is a not-found page, not a broken editor", async ({ page }) => {
  await mockAutomations(page);
  await page.goto(automationEditPath("nope"));
  await expect(page.getByTestId("editor-load-error")).toHaveText("This automation doesn’t exist");
});

test("a server that says nothing widens now wins over the old dialog's labels", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation()],
    widenOnPatch: ["higher mission autonomy"],
    patchScopeMovedOnce: true,
    patchScopeMovedWidened: [],
  });
  await page.goto(automationEditPath("a1"));
  await page.getByTestId("mission-autonomy").selectOption("dispatch");
  await page.getByTestId("editor-save").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog.getByTestId("consent-widened")).toContainText("higher mission autonomy");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog.getByTestId("consent-scope")).toContainText("Checklist: audit");
  await expect(dialog).toContainText("The scope changed again since you opened this");
  await expect(dialog.getByTestId("consent-widened")).toHaveCount(0);
  await expect(dialog.locator("[data-widened]")).toHaveCount(0);
});

test("moving from one automation's editor to another's never saves into the first", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [automation(), automation({ id: "a2", name: "Second automation" })],
    delayGetMs: { a1: 1500 },
  });
  await page.goto(AUTOMATIONS_PATH);
  // In-app navigation, so the editor route changes id without a page load.
  const go = (to: string) =>
    page.evaluate((p) => {
      window.history.pushState({}, "", p);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, to);
  await go(automationEditPath("a1"));
  // A's load is on the wire (and held) before the route moves to B.
  await expect.poll(() => fake.requests.some((r) => r.method === "GET" && r.path === "/a1")).toBe(true);
  await go(automationEditPath("a2"));
  await expect(page.getByTestId("automation-name")).toHaveValue("Second automation");
  // Let A's slow answer land, then edit and save what is on screen.
  await page.waitForTimeout(2000);
  await expect(page.getByTestId("automation-name")).toHaveValue("Second automation");
  await page.getByTestId("automation-name").fill("Second automation, renamed");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const patches = fake.requests.filter((r) => r.method === "PATCH").map((r) => r.path);
  expect(patches).toEqual(["/a2"]);
});

test("the form is locked while a save is in flight, and then shows what the server saved", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation({ state: "off", enabled: false, consented_at: null, next_run: null })],
    patchDelayMs: 1500,
  });
  await page.goto(automationEditPath("a1"));
  const name = page.getByTestId("automation-name");
  await name.fill("Renamed while saving");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-saving")).toBeVisible();
  await expect(page.getByTestId("editor-fields")).toHaveAttribute("disabled", "");
  await expect(name).toBeDisabled();
  await expect(page.getByTestId("message-text")).toBeDisabled();
  await expect(page.getByTestId("editor-save")).toBeDisabled();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  await expect(name).toBeEnabled();
  await expect(name).toHaveValue("Renamed while saving");
  // Nothing unsaved: the form IS the server's state.
  await expect(page.getByTestId("editor-save")).toBeDisabled();
  await expect(page.getByTestId("editor-draft")).toHaveCount(0);
});

test("a long message in the consent dialog is shown in full — its last characters are on screen", async ({ page }) => {
  const long = "check the lockfile. ".repeat(150) + "END-OF-INSTRUCTION-7f3"; // > 3000 characters
  await mockAutomations(page, {
    automations: [
      automation({
        state: "off",
        enabled: false,
        consented_at: null,
        next_run: null,
        action: {
          kind: "start_mission",
          project_id: "p1",
          instruction: { text: long },
          checklist_id: null,
          autonomy: "propose",
        },
      }),
    ],
  });
  await page.goto(automationPath("a1"));
  await page.getByTestId("runs-enable").click();
  const scope = page.getByTestId("consent-scope");
  await expect(scope).toContainText(long);
  await expect(page.getByTestId("consent-show-all")).toHaveCount(0);
  // Not merely in the DOM: the LAST characters are painted, reachable by scrolling the dialog, and
  // nothing clips them — before the box is ticked.
  const seen = await scope.evaluate((ul) => {
    const span = [...ul.querySelectorAll("span")].find((s) => s.textContent?.includes("END-OF-INSTRUCTION-7f3"));
    if (!span || !span.firstChild) return "no span";
    const text = span.firstChild as Text;
    const range = document.createRange();
    range.setStart(text, text.length - 22);
    range.setEnd(text, text.length);
    (span as HTMLElement).scrollIntoView({ block: "end" });
    const r = range.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return hit && (hit === span || span.contains(hit)) ? "visible" : `covered by ${hit?.tagName}`;
  });
  expect(seen).toBe("visible");
  await expect(page.getByTestId("consent-agree")).not.toBeChecked();
});

test("a stale delete whose refresh fails stays open with the error, never reported gone", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation()],
    deleteStaleOnce: true,
    listFailAfterFirst: 500,
  });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("automation-more").first().click();
  await page.getByTestId("automation-menu").getByRole("menuitem", { name: "Delete…" }).click();
  const dialog = page.getByRole("dialog", { name: /Delete/ });
  await dialog.getByRole("button", { name: "Delete" }).click();
  await expect(dialog).toContainText("reading it again failed");
  await expect(dialog).toContainText("Nothing was deleted");
  await expect(page.getByTestId("automations-notice")).toHaveCount(0);
});

test("a run of another automation is never shown under this one's history", async ({ page }) => {
  await mockAutomations(page, {
    automations: [automation(), automation({ id: "a2", name: "Second automation" })],
    runs: {
      a1: [pastRun("r1", "ok", "mission done", 3600)],
      a2: [pastRun("rb", "failed", "B's private failure", 600, { automation_id: "a2", inputs: { message: "B-SECRET-INPUT" } })],
    },
  });
  await page.goto(automationPath("a1", "rb"));
  const elsewhere = page.getByTestId("run-elsewhere");
  await expect(elsewhere).toContainText("This run belongs to another automation");
  await expect(elsewhere.getByRole("link")).toHaveAttribute("href", "/mission/automations/a2?run=rb");
  await expect(page.getByTestId("run-detail")).toHaveCount(0);
  await expect(page.getByText("B-SECRET-INPUT")).toHaveCount(0);
  await expect(page.getByText("B's private failure")).toHaveCount(0);
});

test("double-clicking Load older runs loads each page once, and every run stays reachable", async ({ page }) => {
  const many = Array.from({ length: 120 }, (_, i) =>
    pastRun(`r${String(120 - i).padStart(3, "0")}`, "ok", `run ${120 - i}`, 60 * (i + 1)),
  );
  await mockAutomations(page, { automations: [automation()], runs: { a1: many }, runsPageDelayMs: 800 });
  await page.goto(automationPath("a1"));
  const rows = page.getByTestId("run-row");
  await expect(rows).toHaveCount(50);
  await page.getByTestId("runs-load-more").dblclick();
  await expect(rows).toHaveCount(100);
  await page.getByTestId("runs-load-more").click();
  await expect(rows).toHaveCount(120);
  const ids = await rows.evaluateAll((els) => els.map((e) => e.textContent));
  expect(new Set(ids).size).toBe(120);
  await expect(page.getByTestId("runs-load-more")).toHaveCount(0);
});

test("leaving the editor during a create never pulls the operator back", async ({ page }) => {
  await mockAutomations(page, { createDelayMs: 1500 });
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("automation-name").fill("Made while leaving");
  await page.getByTestId("mission-project").selectOption("p1");
  await page.getByTestId("message-text").fill("Audit the dependencies");
  await page.getByTestId("editor-save").click();
  await page.evaluate((p) => {
    window.history.pushState({}, "", p);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, AUTOMATIONS_PATH);
  await expect(page.getByTestId("automations-page")).toBeVisible();
  await page.waitForTimeout(2500);
  await expect(page).toHaveURL(new RegExp(`${AUTOMATIONS_PATH}$`));
});

test("an out-of-date approval: Run now says so, offers Review and approve, and runs after it", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [automation({ name: "Stand-up nudge", trigger: { kind: "manual" }, next_run: null, action: SESSION_ACTION })],
    legacyReceipt: ["a1"],
  });
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("automation-run-now").first().click();
  const notice = page.getByTestId("automations-notice");
  await expect(notice).toContainText("approval is out of date");
  await notice.getByTestId("notice-approve").click();
  const dialog = page.getByTestId("consent-dialog");
  await expect(dialog.getByTestId("consent-reason")).toContainText("approval is out of date");
  await dialog.getByTestId("consent-agree").check();
  await dialog.getByTestId("consent-confirm").click();
  await expect(dialog).toBeHidden();
  const row = page.getByTestId("automation-row").first();
  await expect(row.getByTestId("automation-state")).toHaveText("Enabled");
  await row.getByTestId("automation-run-now").click();
  await expect(page.getByTestId("automations-notice")).toContainText("“Stand-up nudge” is running.");
  expect(fake.requests.filter((r) => r.method === "POST" && r.path === "/a1/run")).toHaveLength(2);
});

test("a deleted checklist shows as missing, and Save waits for a live choice", async ({ page }) => {
  const fake = await mockAutomations(page, {
    automations: [
      automation({
        action: {
          kind: "start_mission",
          project_id: "p1",
          instruction: { text: "Audit the dependencies" },
          checklist_id: "gone",
          autonomy: "propose",
        },
      }),
    ],
  });
  await page.goto(automationEditPath("a1"));
  const select = page.getByTestId("mission-checklist");
  await expect(select.locator("option:checked")).toHaveText("Missing checklist (deleted)");
  await expect(page.getByTestId("editor-problems")).toContainText("The checklist it used was deleted");
  await page.getByTestId("automation-name").fill("Renamed so the form is dirty");
  await expect(page.getByTestId("editor-save")).toBeDisabled();
  await select.selectOption("");
  await expect(page.getByTestId("editor-save")).toBeEnabled();
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const body = [...fake.requests].reverse().find((r) => r.method === "PATCH")!.body as {
    action: { checklist_id: string | null };
  };
  expect(body.action.checklist_id).toBeNull();
});
