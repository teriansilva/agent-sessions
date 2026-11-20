/** The Forgejo review loop's UI (#2202 PR 3), in a real browser on desktop and phone, both themes.
 *
 *  The editor pairs the "Forgejo webhook" trigger with the "Review loop" action (choosing either
 *  chooses both; leaving either leaves both), offers only API-client specialists as the reviewer and
 *  pins the current version, and saves exactly the strict body the server validates. Once saved,
 *  "Connect Forgejo" shows the payload URL, reveals the secret ONCE after Create secret (Copy, then
 *  Done forgets it), and Rotate says the old one keeps working for 24 h. The run history shows the
 *  new outcomes (`ignored`, `capped`, `undelivered`) with their leg and round, the unsigned headers as
 *  "recorded, untrusted", and no Run now. Every new control holds 44 px; nothing clips at 390 px.
 *  The API is the stateful fake in `./automations.ts`; what the page SENT is asserted. */
import { expect, test, type Page } from "@playwright/test";

import { AUTOMATION_NEW_PATH, AUTOMATIONS_PATH, automationEditPath, automationPath } from "../src/lib/routes";
import { automation, hookState, mockAutomations, type FakeOptions } from "./automations";
import { animationsSettled } from "./nightjarShell";
import { ROSTER, mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// Engine ids from the roster fixture, never literals: one API client, one terminal client.
const API = ROSTER.engines.find((e) => e.runtime === "api" && (e.status ?? "active") === "active")!.id;
const PTY = ROSTER.engines.find((e) => e.runtime === "pty")!.id;

function specialist(id: string, title: string, engine: string, version: number) {
  return {
    id,
    revision: 1,
    archived: false,
    current: { version, title, summary: "", skills: [], engine, model: "mimo-v2.6-pro", effort: "high", capacity: 2, created_at: 1 },
    grade: "strong",
    live: 0,
    skills_missing: [],
    skills_outdated: [],
  };
}
const SPECIALISTS = [
  specialist("shop-reviewer", "Shop Reviewer", API, 3),
  specialist("terminal-reviewer", "Terminal Reviewer", PTY, 1),
];

const LOOP_ACTION = {
  kind: "forge_review_loop",
  project_id: "p1",
  repo: "example-org/shop",
  reviewer: { specialist_id: "shop-reviewer", version: 3 },
  reviewer_login: "shop-reviewer-bot",
  legs: { issues: true, pulls: true },
  max_rounds: { issue: 5, pr: 4 },
  return_to_origin: true,
};
const LOOP = {
  id: "rv1",
  name: "Review loop · example-org/shop",
  trigger: { kind: "webhook", scheme: "forgejo" },
  action: LOOP_ACTION,
  policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
  next_run: null,
};

const T = Math.floor(Date.now() / 1000) - 600;
const FACTS = { headers: "recorded, not trusted", delivery: "8d2f0c1a" };
function loopRun(id: string, at: number, over: Record<string, unknown>) {
  return {
    id,
    automation_id: "rv1",
    trigger: "webhook",
    slot: `slot:${id}`,
    fire_at: null,
    catch_up: false,
    covered: 1,
    state: "done",
    mission_id: null,
    session_key: null,
    created_at: at,
    finished_at: at + 3,
    scope: {},
    steps: [],
    ...over,
  };
}
const RUNS = [
  loopRun("u1", T + 400, {
    outcome: "undelivered",
    result_class: "failed",
    reason: "the verdict on pr#44 was not delivered: the session that opened it is a terminal session",
    inputs: { ...FACTS, event: "pull_request_review_rejected", leg: "return", item: "pr#44", revision: "51ab7d3e9f00112233445566778899aabbccddee", round: 1, verdict: "changes", record: "rec44" },
  }),
  loopRun("s1", T + 300, {
    outcome: "ok",
    result_class: "ok",
    reason: "Shop Reviewer started on pr#41",
    session_key: `${API}:ses_reviewer41`,
    inputs: { ...FACTS, event: "pull_request", item: "pr#41", revision: "9c1e2f0aa11bb22cc33dd44ee55ff66778899aab", round: 2, cap: 4, record: "rec41" },
  }),
  loopRun("i1", T + 200, {
    outcome: "ignored",
    result_class: "skipped",
    reason: "ignored: stale verdict — it is for pr#41 at 51ab7d3, but the forge says the current revision is 9c1e2f0",
    inputs: { ...FACTS, event: "pull_request_review_approved", leg: "return", item: "pr#41", revision: "51ab7d3", record: "rec40" },
  }),
  loopRun("c1", T + 100, {
    outcome: "capped",
    result_class: "skipped",
    reason: "pr#37 has had its 4 reviews; no review started",
    inputs: { ...FACTS, event: "pull_request", item: "pr#37", revision: "deadbeefcafe0000111122223333444455556666", reviews: 4, cap: 4 },
  }),
];

async function setup(page: Page, opts: FakeOptions = {}) {
  const fake = await mockAutomations(page, { engines: [{ id: API, ok: true, reason: "" }], ...opts });
  await mockRoster(page); // registered last: wins over the common mock
  await page.route("**/api/specialists", (r) =>
    r.fulfill({ json: { specialists: SPECIALISTS, templates: [] } }),
  );
  return fake;
}

async function minHeight(page: Page, testIds: string[]) {
  for (const id of testIds) {
    const box = await page.getByTestId(id).first().boundingBox();
    expect(box, `${id} is on screen`).not.toBeNull();
    expect(box!.height, `${id} is a 44px target`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

/** Every control in `root` holds 44px; neither the document nor the page scrolls sideways. */
async function targetsAndOverflow(page: Page, root: string) {
  const report = await page.evaluate((sel) => {
    const scope = document.querySelector(sel)!;
    const small: string[] = [];
    for (const el of scope.querySelectorAll<HTMLElement>("button, select, input:not([type=checkbox]), a, label")) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
      if (el.tagName === "A" && el.closest("p, [role=status], [role=alert]")) continue;
      // A field's caption label is not a target; a checkbox's label (the tap area) is.
      if (el.tagName === "LABEL" && !el.querySelector("input[type=checkbox]")) continue;
      const tooNarrow = el.tagName === "BUTTON" && r.width < 44;
      if (r.height < 44 || tooNarrow)
        small.push(`${el.tagName} "${(el.textContent || "").trim().slice(0, 30)}" ${Math.round(r.width)}×${Math.round(r.height)}`);
    }
    const clipped: string[] = [];
    for (const el of scope.querySelectorAll<HTMLElement>("code, [data-testid]")) {
      const r = el.getBoundingClientRect();
      if (r.width && r.right > document.documentElement.clientWidth + 0.5)
        clipped.push(`${el.tagName}.${el.dataset.testid ?? ""} right=${Math.round(r.right)}`);
    }
    const doc = document.scrollingElement!;
    const pageEl = scope as HTMLElement;
    return {
      small,
      clipped,
      docOverflow: doc.scrollWidth - doc.clientWidth,
      pageOverflow: pageEl.scrollWidth - pageEl.clientWidth,
    };
  }, root);
  expect(report.small, "controls under 44px").toEqual([]);
  expect(report.clipped, "elements past the right edge").toEqual([]);
  expect(report.docOverflow, "document overflows horizontally").toBeLessThanOrEqual(0);
  expect(report.pageOverflow, "the page overflows horizontally").toBeLessThanOrEqual(0);
}

test("create a review loop: the trigger and action pair, only API reviewers, the strict body (#2202)", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await expect(page.getByTestId("automation-editor")).toBeVisible();
  const trigger = page.getByTestId("trigger-kind");
  const action = page.getByTestId("action-kind");

  // Choosing the trigger chooses the action…
  await trigger.getByRole("button", { name: "Forgejo webhook" }).click();
  await expect(action.getByRole("button", { name: "Review loop" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("review-loop-fields")).toBeVisible();
  await expect(page.getByTestId("message-mode")).toHaveCount(0); // no message: BattleLab writes the brief
  // …leaving the action leaves the trigger…
  await action.getByRole("button", { name: "Start a mission" }).click();
  await expect(trigger.getByRole("button", { name: "Schedule" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("review-loop-fields")).toHaveCount(0);
  // …and choosing the action chooses the trigger.
  await action.getByRole("button", { name: "Review loop" }).click();
  await expect(trigger.getByRole("button", { name: "Forgejo webhook" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("trigger-callout")).toContainText("There is no Run now");

  await page.getByTestId("automation-name").fill("Review loop · example-org/shop");
  await page.getByTestId("loop-project").selectOption("p1");
  await page.getByTestId("loop-repo").fill("example-org/shop");
  // Only the API-client specialist is offered; the terminal one is not in the list at all.
  const reviewer = page.getByTestId("loop-reviewer");
  await expect(reviewer.locator("option", { hasText: "Shop Reviewer v3" })).toHaveCount(1);
  await expect(reviewer.locator("option", { hasText: "Terminal Reviewer" })).toHaveCount(0);
  await reviewer.selectOption("shop-reviewer");
  await expect(page.getByTestId("loop-reviewer-pin")).toContainText("Pinned at version 3");
  await page.getByTestId("loop-login").fill("shop-reviewer-bot");
  await page.getByTestId("loop-issues").uncheck();
  await page.getByTestId("loop-rounds-pr").selectOption("6");
  await expect(page.getByTestId("editor-problems")).toHaveCount(0);
  await minHeight(page, [
    "loop-project",
    "loop-repo",
    "loop-reviewer",
    "loop-login",
    "loop-rounds-pr",
    "loop-rounds-issue",
  ]);
  for (const id of ["loop-issues", "loop-pulls", "loop-return"]) {
    const label = page.getByTestId(id).locator("xpath=ancestor::label");
    expect((await label.boundingBox())!.height, `${id}'s label is a 44px target`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await expect(page.getByTestId("webhook-after-save")).toBeVisible();

  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("is off until you enable it");
  const create = fake.requests.find((r) => r.method === "POST" && r.path === "")!;
  expect(create.body).toEqual({
    name: "Review loop · example-org/shop",
    trigger: { kind: "webhook", scheme: "forgejo" },
    action: { ...LOOP_ACTION, legs: { issues: false, pulls: true }, max_rounds: { issue: 5, pr: 6 } },
    policy: { concurrency: "allow", max_concurrent: 3, max_runs_per_day: 48, pause_after_failures: 3, expires_at: null },
  });
  // Saved: the forge can be connected now.
  await expect(page.getByTestId("webhook-panel")).toBeVisible();
});

test("Connect Forgejo: the URL, a secret shown once, copy, Done forgets it, rotate keeps the old one 24 h (#2202)", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => undefined);
  const fake = await setup(page, { automations: [automation({ ...LOOP, enabled: false, state: "off", consented_at: null })] });
  await page.goto(automationEditPath("rv1"));
  const panel = page.getByTestId("webhook-panel");
  await expect(panel).toBeVisible();
  const origin = new URL(page.url()).origin;
  await expect(panel.getByTestId("webhook-url")).toHaveText(`${origin}/hooks/a/rv1`);
  await expect(panel).toContainText("application/json");
  await expect(panel.getByTestId("webhook-events")).toContainText("Issue comment");
  await expect(panel.getByTestId("webhook-events")).toContainText("Pull request reviewed");
  await expect(panel.getByTestId("webhook-events")).toContainText("Pull request synchronized");
  await expect(panel).toContainText("/hooks/");
  await expect(panel.getByTestId("webhook-last")).toContainText("No delivery yet");
  await expect(panel.getByTestId("webhook-secret-value")).toHaveCount(0);

  const make = panel.getByTestId("webhook-make-secret");
  await expect(make).toHaveText("Create secret");
  await minHeight(page, ["webhook-make-secret"]);
  await make.click();
  const secret = panel.getByTestId("webhook-secret-value");
  await expect(secret).toHaveText(/^[0-9a-f]{64}$/);
  const first = await secret.textContent();
  await expect(panel.getByTestId("webhook-secret")).toContainText(/shown once — copy it now/i);
  await minHeight(page, ["webhook-copy", "webhook-done"]);
  await panel.getByTestId("webhook-copy").click();
  await expect(panel.getByRole("status")).toBeVisible(); // "Copied." or the manual-copy fallback

  await panel.getByTestId("webhook-done").click();
  await expect(secret).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(first!);
  await expect(make).toHaveText("Rotate secret");
  await expect(panel).toContainText("old secret keeps working for 24 h");

  await make.click();
  await expect(secret).toHaveText(/^[0-9a-f]{64}$/);
  expect(await secret.textContent()).not.toBe(first);
  await expect(panel.getByTestId("webhook-previous")).toContainText("previous secret keeps working until");
  // Two POSTs, and the secret was never read back with a GET.
  expect(fake.requests.filter((r) => r.path === "/rv1/webhook-secret").map((r) => r.method)).toEqual(["POST", "POST"]);
});

test("run history: ignored, capped and undelivered with leg and round; no Run now (#2202)", async ({ page }) => {
  await setup(page, {
    automations: [
      automation({
        ...LOOP,
        webhook: hookState("rv1", {
          secret_set: true,
          secret_created_at: T - 3600,
          last_delivery_at: T + 400,
          last_event: "pull_request_review_rejected",
          last_note: "verdict",
          unsigned_24h: 3,
        }),
      }),
    ],
    runs: { rv1: RUNS },
  });
  await page.goto(automationPath("rv1"));
  await expect(page.getByTestId("run-row")).toHaveCount(4);
  // Run now is not offered, and the page says why.
  await expect(page.getByTestId("runs-run-now")).toHaveCount(0);
  await expect(page.getByTestId("runs-run-now-why")).toHaveText(
    "No Run now: it runs only when its webhook delivers an event.",
  );
  await expect(page.getByTestId("webhook-unsigned")).toContainText("3 unsigned deliveries refused");
  await expect(page.getByTestId("webhook-last")).toContainText("pull_request_review_rejected · verdict");

  const row = (i: number) => page.getByTestId("run-row").nth(i);
  await expect(row(0).getByTestId("run-outcome")).toHaveText("Not delivered");
  await expect(row(0).getByTestId("run-leg")).toHaveText("return");
  await expect(row(0).getByTestId("run-round")).toHaveText("review 1");
  await expect(row(0)).toContainText("pr#44 at 51ab7d3");
  await expect(row(1).getByTestId("run-outcome")).toHaveText("OK");
  await expect(row(1).getByTestId("run-leg")).toHaveText("review");
  await expect(row(1).getByTestId("run-round")).toHaveText("review 2 / 4");
  await expect(row(2).getByTestId("run-outcome")).toHaveText("Ignored");
  await expect(row(2).getByTestId("run-leg")).toHaveText("return");
  // The capped row is the review leg (Hermes' copy note on the mockup), with its reviews used.
  await expect(row(3).getByTestId("run-outcome")).toHaveText("Capped");
  await expect(row(3).getByTestId("run-leg")).toHaveText("review");
  await expect(row(3).getByTestId("run-round")).toHaveText("4 / 4 reviews");

  // The detail: item at its full revision, and the unsigned headers marked untrusted.
  const detail = page.getByTestId("run-detail");
  await expect(detail.getByTestId("run-detail-item")).toHaveText("pr#44 at 51ab7d3e9f00112233445566778899aabbccddee");
  await expect(detail.getByTestId("run-detail-delivery")).toContainText("pull_request_review_rejected · delivery 8d2f0c1a");
  await expect(detail.getByTestId("run-detail-delivery")).toContainText("(recorded, untrusted)");
  await row(1).click();
  await expect(detail.getByRole("link", { name: "Open session" })).toBeVisible();

  // The list: Run now is disabled for it, saying why.
  await page.goto(AUTOMATIONS_PATH);
  const listRow = page.locator('[data-testid="automation-row"][data-id="rv1"]');
  await expect(listRow).toContainText("Forgejo webhook");
  await expect(listRow).toContainText("Review loop");
  await expect(listRow.getByTestId("automation-run-now")).toBeDisabled();
  await expect(listRow.getByTestId("automation-run-now")).toHaveAttribute(
    "title",
    "It runs only when its webhook delivers an event",
  );
});

test("a review loop goes through the same server-authored consent to enable (#2202)", async ({ page }) => {
  const fake = await setup(page, { automations: [automation({ ...LOOP, enabled: false, state: "off", consented_at: null })] });
  await page.goto(automationEditPath("rv1"));
  await page.getByTestId("editor-enable").click();
  const consent = page.getByTestId("consent-dialog");
  await expect(consent).toContainText("Starts when Forgejo sends a signed event for example-org/shop");
  await expect(consent).toContainText("Trusts verdicts only from shop-reviewer-bot");
  await consent.getByTestId("consent-agree").check();
  await consent.getByTestId("consent-confirm").click();
  await expect(consent).toBeHidden();
  const sent = [...fake.requests].reverse().find((r) => r.path === "/rv1/enable")!.body!;
  expect(sent).toMatchObject({ consent: true, scope_digest: "digest-1" });
});

test("at 390px: 44px targets and nothing clipped — editor with a revealed secret, run history (#2202)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 860 });
  await setup(page, {
    automations: [automation({ ...LOOP, enabled: false, state: "off", consented_at: null })],
    runs: { rv1: RUNS },
  });
  await page.goto(automationEditPath("rv1"));
  await expect(page.getByTestId("review-loop-fields")).toBeVisible();
  await page.getByTestId("webhook-make-secret").click();
  await expect(page.getByTestId("webhook-secret-value")).toBeVisible();
  await targetsAndOverflow(page, '[data-testid="automation-editor"]');

  await page.goto(automationPath("rv1"));
  await expect(page.getByTestId("run-row")).toHaveCount(4);
  await targetsAndOverflow(page, '[data-testid="automation-runs"]');
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the editor, Connect Forgejo and the run history — ${theme} (#2202)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const mobile = info.project.name === "mobile";
    await page.setViewportSize(mobile ? { width: 390, height: 3900 } : { width: 1440, height: 1900 });
    await setup(page, {
      automations: [automation({ ...LOOP, enabled: false, state: "off", consented_at: null })],
      runs: { rv1: RUNS },
    });
    await page.goto(automationEditPath("rv1"));
    await expect(page.getByTestId("review-loop-fields")).toBeVisible();
    await page.getByTestId("webhook-make-secret").click();
    await expect(page.getByTestId("webhook-secret-value")).toBeVisible();
    await animationsSettled(page);
    await page.screenshot({ path: info.outputPath(`impl-editor-${info.project.name}-${theme}.png`) });
    await page.goto(automationPath("rv1"));
    await expect(page.getByTestId("run-row")).toHaveCount(4);
    await expect(page.getByTestId("run-detail")).toBeVisible();
    await animationsSettled(page);
    await page.screenshot({ path: info.outputPath(`impl-runs-${info.project.name}-${theme}.png`) });
  });
}
