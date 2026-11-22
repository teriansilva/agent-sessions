/** Library → Actions and the wizard's action picker (#2248 P1), in a real browser on the desktop and
 *  mobile projects, both themes.
 *
 *  The gallery: the five actions as cards (category, effect pips, name, line, requirement, used by),
 *  search and category chips that filter them, a requirement not met in its own words, a failed read
 *  that says so, 44 px targets under a finger, one frame per card and nothing past the edge at 390
 *  px. The wizard's What step: the same list as a compact searchable picker; each of the five can be
 *  picked under the trigger that starts it, and what it saves is what the fixed cards saved. The
 *  gallery is the server's own answer (`fixtures/actions-gallery.json`, pinned by pytest). */
import { expect, test, type Page } from "@playwright/test";

import { ACTIONS_PATH, AUTOMATIONS_PATH, AUTOMATION_NEW_PATH, automationEditPath } from "../src/lib/routes";
import { ACTIONS_API } from "../src/lib/apiPaths";
import type { ActionGallery } from "../src/types/actions";
import type { Route } from "@playwright/test";
import { ACTION_GALLERY, mockActions } from "./actions";
import {
  PROJECT,
  automation,
  mockAutomations,
  targetsAndOverflow,
  toStep,
  wizardHeading,
  wizardNext,
} from "./automations";
import { animationsSettled } from "./nightjarShell";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";
import { mockWebhooks, webhook } from "./webhooks";

const HOOK = webhook({ id: "0a1b2c3d4e5f6a7b", name: "shop repo" });
const ENGINES = [
  { id: "claude", ok: true, reason: "" },
  { id: "claude-api", ok: true, reason: "" },
];

async function setup(page: Page, gallery?: Parameters<typeof mockActions>[1]) {
  const fake = await mockAutomations(page, { engines: ENGINES });
  await mockWebhooks(page, [HOOK]);
  await mockRoster(page);
  const actions = gallery ? await mockActions(page, gallery) : null;
  return { fake, actions };
}

const cards = (page: Page) => page.getByTestId("action-card");
const ids = (page: Page) =>
  cards(page).evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.id));

/** At 390 px on the phone project: the width the issue names. */
async function phoneWidth(page: Page, mobile: boolean) {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
}

/** No frame inside a frame: each card is one frame, and the picker's rows are plain rows. */
async function oneFrame(page: Page, root: string) {
  const nested = await page.evaluate(
    (sel) =>
      document.querySelectorAll(
        `${sel} :is(.nj-frame, .nj-frame-sm, .nj-panel) :is(.nj-frame, .nj-frame-sm, .nj-panel)`,
      ).length,
    root,
  );
  expect(nested, "a frame inside a frame").toBe(0);
}

test("the gallery lists the five as cards with their facts, and search and chips filter them", async ({ page }, info) => {
  const mobile = info.project.name === "mobile";
  await phoneWidth(page, mobile);
  await setup(page);
  await page.goto(ACTIONS_PATH);
  await expect(page.getByRole("heading", { level: 1, name: "Actions" })).toBeVisible();
  // Library is the section, Actions the entry (`sections.ts`).
  await expect(cards(page)).toHaveCount(5);
  expect(await ids(page)).toEqual(ACTION_GALLERY.actions.map((a) => a.id));

  const session = cards(page).filter({ hasText: "Start a session" });
  await expect(session).toContainText("BattleLab");
  await expect(session.getByTestId("action-effect")).toHaveText("Write · BattleLab");
  await expect(session.getByTestId("action-effect")).toHaveAttribute("data-effect", "write-battlelab");
  await expect(session.locator("[data-effect] i")).toHaveCount(5);
  await expect(session.getByTestId("action-requires")).toHaveText("Ready");
  await expect(session.getByTestId("action-used-by")).toHaveText("used by 3");
  const mission = cards(page).filter({ hasText: "Start a mission" });
  await expect(mission.getByTestId("action-used-by")).toHaveText("not used yet");
  // What depends on the wizard's choice is said, never shown as missing.
  await expect(mission.getByTestId("action-requires")).toHaveText(/^Ready\s*· needs a project$/);
  const tell = cards(page).filter({ hasText: "Tell HQ" });
  await expect(tell.getByTestId("action-effect")).toHaveText("Notify");

  // The effect is a class, never a state: the tag takes no status colour in either case.
  const colours = await page.getByTestId("action-effect").evaluateAll((els) =>
    [...new Set(els.map((e) => getComputedStyle(e).color))],
  );
  expect(colours).toHaveLength(1);

  // The legend names every effect class, least to most.
  await expect(page.getByTestId("actions-legend").getByTestId("action-effect")).toHaveText([
    "Read",
    "Notify",
    "Write · BattleLab",
    "Write · external",
    "Run · local",
  ]);

  // Chips: All and the categories that have an action. The rest are named as coming later.
  const chips = page.getByRole("group", { name: "Show" }).getByRole("button");
  await expect(chips).toHaveText([/^All\s*5$/, /^BattleLab\s*4$/, /^Forge\s*1$/]);
  await expect(page.getByTestId("actions-coming")).toContainText(
    "More come in later releases: messaging, files, run locally, web.",
  );
  await chips.filter({ hasText: "Forge" }).click();
  await expect(chips.filter({ hasText: "Forge" })).toHaveAttribute("aria-pressed", "true");
  expect(await ids(page)).toEqual(["forge_review_loop"]);
  await chips.filter({ hasText: "All" }).click();

  const search = page.getByRole("searchbox", { name: "Search actions" });
  await search.fill("session");
  expect(await ids(page)).toEqual(["start_session", "send_to_session"]);
  // The count is said (hidden on a phone, where the cards themselves are the count).
  await expect(page.locator("[role=status]", { hasText: /^2 actions$/ })).toHaveCount(1);
  await search.fill("zebra");
  await expect(cards(page)).toHaveCount(0);
  await expect(page.getByTestId("actions-none")).toContainText("No action matches “zebra”");
  await search.fill("");
  await expect(cards(page)).toHaveCount(5);

  await animationsSettled(page);
  await oneFrame(page, '[data-testid="actions-page"]');
  if (mobile) {
    // Under a finger every control holds 44 px.
    for (const el of await page.locator('[data-testid="actions-page"] :is(button, input)').all()) {
      const box = await el.boundingBox();
      if (box) expect(box.height, await el.evaluate((e) => e.outerHTML.slice(0, 60))).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }
  const overflow = await page.evaluate(() => {
    const page = document.querySelector<HTMLElement>('[data-testid="actions-page"]')!;
    const past = [...page.querySelectorAll<HTMLElement>("[data-testid], h1, h2")]
      .filter((e) => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().right > window.innerWidth + 0.5)
      .map((e) => e.dataset.testid ?? e.tagName);
    return { past, doc: document.scrollingElement!.scrollWidth - document.scrollingElement!.clientWidth };
  });
  expect(overflow.past, "elements past the right edge").toEqual([]);
  expect(overflow.doc, "the document scrolls sideways").toBeLessThanOrEqual(0);
});

test("a requirement that is not met says so in its own words, with no status colour on the effect", async ({ page }) => {
  const gallery: ActionGallery = structuredClone(ACTION_GALLERY);
  const s = gallery.actions.find((a) => a.id === "start_session")!;
  s.requires = s.requires.map((r) => ({
    ...r,
    ok: false,
    detail: "needs an API agent that can start unattended (Settings → Agents)",
  }));
  s.available = false;
  await setup(page, gallery);
  await page.goto(ACTIONS_PATH);
  const card = cards(page).filter({ hasText: "Start a session" });
  await expect(card.getByTestId("action-requires")).toHaveText(
    "Needs an API agent that can start unattended (Settings → Agents)",
  );
  await expect(card.locator(".hud-led.att")).toHaveCount(1);
});

test("a gallery that can't be read says so and reads again on Try again", async ({ page }) => {
  const { actions } = await setup(page, { status: 503, detail: "The automations store is unavailable" });
  await page.goto(ACTIONS_PATH);
  await expect(page.getByTestId("actions-error")).toContainText("unavailable");
  await expect(cards(page)).toHaveCount(0);
  const before = actions!.reads();
  await page.getByRole("button", { name: "Try again" }).click();
  await expect.poll(() => actions!.reads()).toBe(before + 1);
});

test("the Library menu leads to Actions, after Automations", async ({ page }) => {
  await setup(page);
  await page.goto(AUTOMATIONS_PATH);
  if ((await page.locator(".app.navOpen").count()) > 0) await page.keyboard.press("Escape");
  await page.locator('.hud-topbar [data-testid="section-menu-library"]').click();
  const menu = page.locator('[data-testid="section-menu-library-panel"]');
  const labels = await menu.locator("a[data-subsection]").allTextContents();
  expect(labels.indexOf("Actions")).toBe(labels.indexOf("Automations") + 1);
  await menu.locator('a[data-subsection="actions"]').click();
  await expect(page).toHaveURL(new RegExp(`${ACTIONS_PATH}$`));
  await expect(cards(page)).toHaveCount(5);
  // Library is lit, and Actions is the page you are on.
  await expect(page.locator('.hud-topbar [data-section="library"]')).toHaveAttribute("aria-current", "true");
  await page.locator('.hud-topbar [data-testid="section-menu-library"]').click();
  await expect(
    page.locator('[data-testid="section-menu-library-panel"] a[data-subsection="actions"]'),
  ).toHaveAttribute("aria-current", "page");
});

// ---- the wizard's What step ---------------------------------------------------------------------

test("the What step picks each action from the gallery under the trigger that starts it", async ({ page }, info) => {
  const mobile = info.project.name === "mobile";
  await phoneWidth(page, mobile);
  const { fake } = await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  const group = page.getByTestId("action-kind");
  // A schedule: the four it starts, in the gallery's order; a review loop is a webhook's.
  await expect(group.getByRole("button")).toHaveText([
    /Start a mission/,
    /Start a session/,
    /Message a session/,
    /Tell HQ/,
  ]);
  await expect(group.getByTestId("action-effect")).toHaveText([
    "Write · BattleLab",
    "Write · BattleLab",
    "Write · BattleLab",
    "Notify",
  ]);
  for (const id of ["start_session", "send_to_session", "tell_hq", "start_mission"]) {
    await page.getByTestId(`action-kind-${id}`).click();
    await expect(page.getByTestId(`action-kind-${id}`)).toHaveAttribute("aria-pressed", "true");
    await expect(group.locator('[aria-pressed="true"]')).toHaveCount(1);
  }
  // Search narrows the list; the picked one stays listed.
  const search = page.getByTestId("action-kind-search");
  await search.fill("session");
  await expect(group.getByRole("button")).toHaveText([/Start a mission/, /Start a session/, /Message a session/]);
  await search.fill("");

  await animationsSettled(page);
  await oneFrame(page, '[data-testid="action-kind"]');
  await targetsAndOverflow(page, "main");

  // What it saves is what the fixed cards saved: a mission, as before.
  await page.getByTestId("mission-project").selectOption(PROJECT.id);
  await page.getByTestId("message-text").fill("Audit the dependencies");
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill("Audit");
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-notice")).toContainText("Saved");
  const created = fake.requests.find((r) => r.method === "POST" && r.path === "")!.body!;
  expect((created.action as { kind: string }).kind).toBe("start_mission");
});

test("a webhook's What step offers the review loop and never a mission; chips filter it", async ({ page }) => {
  await setup(page);
  await page.goto(AUTOMATION_NEW_PATH);
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(HOOK.id);
  await toStep(page, "What does it do?");
  const group = page.getByTestId("action-kind");
  await expect(group.getByRole("button")).toHaveText([
    /Start a session/,
    /Message a session/,
    /Review loop/,
    /Tell HQ/,
  ]);
  await page.getByTestId("action-kind-chips").getByRole("button", { name: "Forge" }).click();
  await expect(group.getByRole("button")).toHaveText([/Start a session/, /Review loop/]);
  await page.getByTestId("action-kind-forge_review_loop").click();
  await expect(page.getByTestId("action-kind-forge_review_loop")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("loop-return")).toBeVisible();
  await page.getByTestId("action-kind-chips").getByRole("button", { name: "All" }).click();
  await expect(group.getByRole("button")).toHaveCount(4);
  await expect(wizardHeading(page)).toHaveText("What does it do?");
});

test("a picker that can't read the gallery says so and offers Retry", async ({ page }) => {
  const { actions } = await setup(page, { status: 503, detail: "The automations store is unavailable" });
  await page.goto(AUTOMATION_NEW_PATH);
  await toStep(page, "What does it do?");
  await expect(page.getByTestId("action-kind-error")).toContainText("Couldn’t load the actions");
  await expect(page.getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  const before = actions!.reads();
  await page.getByTestId("action-kind-error").getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => actions!.reads()).toBe(before + 1);
});

// ---- the gallery's readiness holds the wizard (#2248 P1, Hermes on PR #2266) --------------------

/** `GET /api/actions` that fails while `fail.on` is set, else answers the server's gallery.
 *  Registered last, so it wins over the fake's default. */
async function switchableActions(page: Page) {
  const fail = { on: true, reads: 0 };
  await page.route(`**${ACTIONS_API}`, (r) => {
    fail.reads++;
    return fail.on
      ? r.fulfill({ status: 503, json: { detail: "The automations store is unavailable" } })
      : r.fulfill({ json: ACTION_GALLERY });
  });
  return fail;
}

const next = (page: Page) => page.getByRole("button", { name: "Next", exact: true });
/** The rail's entry for a step: a button only when it can be jumped to. Hidden ones count too:
 *  the phone hides the rail, and the same `canJump` decides what it would offer. */
const railJump = (page: Page, label: RegExp) =>
  page
    .locator("nav[aria-label$=' steps']")
    .getByRole("button", { name: label, includeHidden: true });

/** The What step is held by the gallery: Next, the rail past What, and so the save, are off. */
async function heldByTheGallery(page: Page) {
  await expect(page.getByTestId("action-kind-error")).toBeVisible();
  await expect(page.getByTestId("action-kind-error").getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(next(page)).toBeDisabled();
  await expect(railJump(page, /Limits/)).toHaveCount(0);
  await expect(railJump(page, /Review/)).toHaveCount(0);
}

/** Retry succeeds: the hold lifts — Next, the rail to Review, and Save there. */
async function releasedByRetry(page: Page, fail: { on: boolean }) {
  fail.on = false;
  await page.getByTestId("action-kind-error").getByRole("button", { name: "Retry" }).click();
  await expect(page.getByTestId("action-kind-error")).toHaveCount(0);
  await expect(next(page)).toBeEnabled();
  await expect(railJump(page, /Review/)).toHaveCount(1);
}

test("a failed gallery read holds a complete start-session form: Next, the rail and save", async ({ page }) => {
  const { fake } = await setup(page);
  await page.route("**/api/fs/dirs**", (r) => r.fulfill({ json: { home: "/repo", path: "/repo", dirs: [] } }));
  const fail = await switchableActions(page);
  await page.goto(AUTOMATION_NEW_PATH);
  // A webhook trigger makes the action a new session, so its fields show without the list.
  await page.getByTestId("trigger-kind").getByRole("button", { name: /When a webhook delivers/ }).click();
  await page.getByTestId("trigger-webhook").selectOption(HOOK.id);
  await toStep(page, "What does it do?");
  await page.getByTestId("session-engine").selectOption("claude-api");
  await page.getByRole("button", { name: "Choose…" }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Select/ }).click();
  await expect(page.getByTestId("session-folder")).toHaveValue("/repo");
  await page.getByTestId("message-text").fill("Review the pull request");
  await heldByTheGallery(page);
  expect(fake.requests.filter((r) => r.method !== "GET")).toEqual([]);

  await releasedByRetry(page, fail);
  await page.getByTestId("action-kind-start_session").click();
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill("Review PRs");
  await expect(page.getByTestId("editor-save")).toBeEnabled();
});

test("editing a complete automation with a failed gallery read holds it the same way", async ({ page }) => {
  const saved = automation({
    id: "s1",
    name: "Morning session",
    action: {
      kind: "start_session",
      engine: "claude-api",
      model: null,
      folder: "/repo",
      bypass: false,
      message: { text: "Good morning" },
    },
  });
  const fake = await mockAutomations(page, { engines: ENGINES, automations: [saved] });
  await mockWebhooks(page, [HOOK]);
  await mockRoster(page);
  const fail = await switchableActions(page);
  await page.goto(automationEditPath("s1"));
  await expect(wizardHeading(page)).toHaveText("When does it run?");
  // Nothing past What can be reached while the gallery can't be read.
  await expect(railJump(page, /Review/)).toHaveCount(0);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  await heldByTheGallery(page);
  expect(fake.requests.filter((r) => r.method !== "GET")).toEqual([]);

  await releasedByRetry(page, fail);
  await expect(page.getByTestId("action-kind-start_session")).toHaveAttribute("aria-pressed", "true");
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill("Morning session, renamed");
  await expect(page.getByTestId("editor-save")).toBeEnabled();
});

test("a cached gallery whose refresh fails shows the error and Retry, and holds the step", async ({ page }) => {
  await setup(page);
  const fail = await switchableActions(page);
  fail.on = false;
  await page.goto(AUTOMATIONS_PATH);
  await page.getByTestId("automation-new").click();
  await toStep(page, "What does it do?");
  await expect(page.getByTestId("action-kind").getByRole("button")).toHaveCount(4);
  // Leave inside the app (the answer stays cached), then come back with the gallery failing.
  await page.getByTestId("editor-cancel").click();
  await expect(page.getByTestId("automation-new")).toBeVisible();
  fail.on = true;
  const before = fail.reads;
  await page.getByTestId("automation-new").click();
  await toStep(page, "What does it do?");
  await expect.poll(() => fail.reads).toBeGreaterThan(before);
  // The cached list may stay; the problem and Retry must show, and hold the step.
  await heldByTheGallery(page);
  await page.getByTestId("mission-project").selectOption(PROJECT.id);
  await page.getByTestId("message-text").fill("Audit the dependencies");
  await expect(next(page)).toBeDisabled();
  await releasedByRetry(page, fail);
});

/** `GET /api/actions` answered at once the first time, then left PENDING until the test settles it:
 *  a slow refresh of a cached list. */
async function heldRefresh(page: Page) {
  const pending: Route[] = [];
  let reads = 0;
  await page.route(`**${ACTIONS_API}`, (r) => {
    reads++;
    if (reads === 1) return r.fulfill({ json: ACTION_GALLERY });
    pending.push(r);
    return undefined;
  });
  return {
    pending: () => pending.length,
    resolve: () => Promise.all(pending.splice(0).map((r) => r.fulfill({ json: ACTION_GALLERY }))),
    reject: () =>
      Promise.all(
        pending.splice(0).map((r) =>
          r.fulfill({ status: 503, json: { detail: "The automations store is unavailable" } }),
        ),
      ),
  };
}

/** Edit an otherwise-valid saved automation a second time, its gallery refresh still pending. */
async function editAgainWhileRefreshing(page: Page) {
  const saved = automation({
    id: "s1",
    name: "Morning session",
    action: {
      kind: "start_session",
      engine: "claude-api",
      model: null,
      folder: "/repo",
      bypass: false,
      message: { text: "Good morning" },
    },
  });
  await mockAutomations(page, { engines: ENGINES, automations: [saved] });
  await mockWebhooks(page, [HOOK]);
  await mockRoster(page);
  const refresh = await heldRefresh(page);
  // First visit: the gallery answers, and the answer is kept.
  await page.goto(automationEditPath("s1"));
  await toStep(page, "What does it do?");
  await expect(page.getByTestId("action-kind-start_session")).toHaveAttribute("aria-pressed", "true");
  // Leave inside the app, and come back: the second read stays pending.
  await page.getByTestId("editor-cancel").click();
  await page.getByRole("link", { name: "Edit", exact: true }).click();
  await expect(wizardHeading(page)).toHaveText("When does it run?");
  await expect.poll(refresh.pending).toBe(1);
  return refresh;
}

/** Held by a refresh still in flight: the rail can't reach Review, What's Next is off, and the
 *  note is calm (no error); the cached list stays. */
async function heldWhileChecking(page: Page) {
  await expect(railJump(page, /Review/)).toHaveCount(0);
  await wizardNext(page);
  await expect(wizardHeading(page)).toHaveText("What does it do?");
  await expect(page.getByTestId("action-kind-checking")).toContainText("Checking the action list");
  await expect(page.getByTestId("action-kind-error")).toHaveCount(0);
  await expect(page.getByTestId("action-kind").getByRole("button")).toHaveCount(4);
  await expect(next(page)).toBeDisabled();
  await expect(railJump(page, /Limits/)).toHaveCount(0);
  await expect(railJump(page, /Review/)).toHaveCount(0);
}

test("a pending refresh of a cached gallery holds the step until it answers", async ({ page }) => {
  const refresh = await editAgainWhileRefreshing(page);
  await heldWhileChecking(page);
  await refresh.resolve();
  await expect(page.getByTestId("action-kind-checking")).toHaveCount(0);
  await expect(next(page)).toBeEnabled();
  await expect(railJump(page, /Review/)).toHaveCount(1);
  await toStep(page, "Review and enable");
  await page.getByTestId("automation-name").fill("Morning session, renamed");
  await expect(page.getByTestId("editor-save")).toBeEnabled();
});

test("a pending refresh that then fails shows the error and Retry, and keeps the step held", async ({ page }) => {
  const refresh = await editAgainWhileRefreshing(page);
  await heldWhileChecking(page);
  await refresh.reject();
  await expect(page.getByTestId("action-kind-error")).toBeVisible();
  await expect(page.getByTestId("action-kind-error").getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.getByTestId("action-kind-checking")).toHaveCount(0);
  await expect(next(page)).toBeDisabled();
  await expect(railJump(page, /Review/)).toHaveCount(0);
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the gallery and the picker — ${theme} (#2248)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    const mobile = info.project.name === "mobile";
    await page.setViewportSize(mobile ? { width: 390, height: 1900 } : { width: 1440, height: 1100 });
    await setup(page);
    const shot = async (name: string) => {
      await animationsSettled(page);
      await page.screenshot({ path: info.outputPath(`impl-${name}-${info.project.name}-${theme}.png`) });
    };
    await page.goto(ACTIONS_PATH);
    await expect(cards(page)).toHaveCount(5);
    await shot("gallery");
    await page.goto(AUTOMATION_NEW_PATH);
    await toStep(page, "What does it do?");
    await page.getByTestId("action-kind-start_session").click();
    await shot("picker");
  });
}
