import { expect, type Locator, type Page, test } from "@playwright/test";
import { CID, PID, shell } from "./askOperationShell";
import { openAsk, pickConversation } from "./askSidebar";
import { mockRoster } from "./roster";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

// HQ's subagents (#2398), in a real browser, desktop and phone, dark and light: the driver badge on
// every sidebar row (HQ · task n, You; a mission's is its mission tag) with the phone's metadata
// truncating with a visible ellipsis; the badge in a map window's chrome (its only bar); and the
// conversation's group panel — Open / Take over / Hand back / Stop (44px, focus ring; Stop is the
// operator's own interrupt route), a queued spawn with Cancel, a queued spawn past its deadline
// reading not started, a tainted spawn card and an effect parked after sign-out, the empty state
// (no subagents yet) and a refused tap. Red on main: no badge, no group panel, no Cancel route.

const now = Math.floor(Date.now() / 1000);
const TURN = "11111111-1111-4111-8111-111111111111";
const K1 = "claude-api:aaaaaaaa-1111-4111-8111-111111111111";
const K2 = "codex-api:bbbbbbbb-2222-4222-8222-222222222222";

function row(n: number, extra: Record<string, unknown>) {
  const id = `${n}aaaaaaa-1111-4111-8111-11111111111${n}`;
  return {
    id: `claude-api:${id}`,
    engine: "claude-api",
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: "/home/u/web-shop",
    project: {
      kind: "project",
      id: "p1",
      name: "web-shop-localization-and-translations",
      color: "#ffb000",
    },
    last_mtime: now - 60 * n,
    first_user_message: "",
    title: `Subagent session ${n}`,
    sticky: false,
    archived: false,
    ai_summary: "",
    ...extra,
  };
}

const ROWS = [
  row(1, { title: "Researcher: map i18n libraries", driver: { who: "hq", task_label: "task 2", cid: CID } }),
  row(2, { title: "Tester: run i18n suite", driver: { who: "commander" } }),
  row(3, {
    title: "Release 0.21 · step 3",
    driver: { who: "mission" },
    mission: { id: "msn_" + "c".repeat(32), title: "Release 0.21", state: "running" },
  }),
];

async function mockSessions(page: Page, rows = ROWS) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/**/draft", (r) =>
    r.fulfill({ json: { id: "d", text: "", attachments: [], updated_at: null } }),
  );
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "web-shop-localization-and-translations", color: "#ffb000", archived: false }] } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/sessions?**", (r) =>
    r.fulfill({
      json: { sessions: rows, next_offset: null, total: rows.length, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: ["project:p1"],
        projects_hidden: [],
        project_names: {},
        compose_default: "collapsed",
      },
    }),
  );
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    ws.send(JSON.stringify({ t: "role", role: "owner" }));
    ws.send(JSON.stringify({ t: "seq", n: 0 }));
    ws.send(Buffer.from(`\x1b[2J\x1b[H${"subagent window\r\n".repeat(20)}`));
  });
}

async function openSessionDrawer(page: Page) {
  const trigger = page.getByRole("button", { name: /Open session list/i });
  if (!(await trigger.count())) return;
  await trigger.first().click();
  await page.getByRole("dialog").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForFunction(
    () => {
      const el = document.querySelector("aside.sidebar");
      return el !== null && el.getBoundingClientRect().x >= 0;
    },
    undefined,
    { timeout: 5000 },
  );
}

async function setTheme(page: Page, theme: string) {
  await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
}

async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

async function target(l: Locator) {
  const box = (await l.boundingBox())!;
  expect(box.height, `${await l.textContent()} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
}

for (const theme of ["dark", "light"] as const) {
  test(`every sidebar row shows who drives it; the phone truncates with an ellipsis · ${theme}`, async ({ page }, info) => {
    await mockSessions(page);
    await page.goto(NEW_SESSION_PATH);
    await setTheme(page, theme);
    await openSessionDrawer(page);
    const rowOf = (title: string) =>
      page.locator("aside.sidebar a").filter({ hasText: title }).first();
    const hq = rowOf("Researcher: map i18n libraries").getByTestId("driver-badge");
    await expect(hq).toHaveText("HQ · task 2");
    await expect(hq).toHaveAttribute("data-driver", "hq");
    await expect(rowOf("Tester: run i18n suite").getByTestId("driver-badge")).toHaveText("You");
    // A mission's driver is its mission tag: said once.
    const mission = rowOf("Release 0.21 · step 3");
    await expect(mission.getByTestId("row-mission-tag")).toContainText("Release 0.21");
    await expect(mission.getByTestId("driver-badge")).toHaveCount(0);
    // The badge never shrinks; the line's tail ellipsizes instead (Hermes 141658).
    const badgeBox = (await hq.boundingBox())!;
    expect(badgeBox.width).toBeGreaterThan(20);
    const tail = rowOf("Researcher: map i18n libraries").locator('[class*="metaText"]');
    const clip = await tail.evaluate((el) => ({
      overflow: getComputedStyle(el).textOverflow,
      cut: el.scrollWidth > el.clientWidth,
    }));
    expect(clip.overflow).toBe("ellipsis");
    if (info.project.name === "mobile") expect(clip.cut).toBe(true);
    await noSidewaysScroll(page);
    await page.screenshot({ path: info.outputPath(`driver-badges-${theme}.png`) });
  });
}

test("a map window's chrome carries the driver badge in its one bar", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "the map's windows are the desktop layout");
  await mockSessions(page);
  // The API session's pane, as the structured runtime serves it (an idle session HQ drives).
  await page.route(/\/api\/structured\/sessions\/[^/]+(\/[a-z-]+)?(\?.*)?$/, (r) => {
    const sub = new URL(r.request().url()).pathname.split("/").at(-1);
    if (sub === "events")
      return r.fulfill({ json: { session_key: ROWS[0].id, revision: 1, next_cursor: 1, events: [] } });
    return r.fulfill({
      json: {
        session_key: ROWS[0].id, revision: 1, event_cursor: 1, cwd: "/home/u/web-shop",
        state: "idle", active_turn: null, model_requested: null, model_effective: null,
        turns: [], omitted_turns: 0, pending_requests: [],
        native: { native_id: "n1", worker: "w1", background_active: false },
        read_only: null, bypass: false,
        driver: { who: "hq", generation: 1, since: now - 60, can_hand_back: false, task: { n: 2 } },
      },
    });
  });
  await mockRoster(page, {
    overrides: {
      "claude-api": {
        present: true,
        supports_new: true,
        api: { kind: "claude-stream-json", source: "claude", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  await page.goto("/overview");
  await page.getByRole("button", { name: /expand all/i }).click();
  const chip = page.locator(".tr-overview .tr-ov-chip", { hasText: "Researcher: map i18n libraries" });
  await expect(chip).toBeVisible();
  await chip.click();
  const win = page.locator(`[data-session-window="${ROWS[0].id}"]`);
  await expect(win.locator("[data-window-head]")).toBeVisible();
  await expect(win.locator("[data-window-head]")).toHaveCount(1);
  const badge = win.locator("[data-window-head]").getByTestId("driver-badge");
  await expect(badge).toHaveText("HQ · task 2");
  await page.screenshot({ path: info.outputPath("map-chrome-badge.png") });
});

// ---- the conversation's group panel ---------------------------------------------------------------

const SUBAGENTS = [
  { turn_id: TURN, handle: "sp-1", outcome: "started", title: "web-shop", agent: "Claude — API · Researcher v3", key: K1, task_n: 2 },
  { turn_id: TURN, handle: "sp-2", outcome: "started", title: "web-shop", agent: "Codex — API · Tester v2", key: K2, task_n: 2 },
  { turn_id: TURN, handle: "sp-3", outcome: "queued", title: "web-shop", agent: "Claude — API · Reviewer v1", task_n: 2, reason: "queued: session limit 6 / 6" },
  { turn_id: TURN, handle: "sp-4", outcome: "not_started", title: "web-shop", agent: "Claude — API · Translator v1", task_n: 2, reason: "not started: the session limit stayed full" },
];
const LIVE = [
  { ...SUBAGENTS[0], state: "working", active_turn: "t-1", driver: { who: "hq", generation: 1, here: true }, steered: 2 },
  { ...SUBAGENTS[1], state: "taken_over", active_turn: null, driver: { who: "commander", generation: 3, here: true }, steered: 1 },
  { ...SUBAGENTS[2], state: "queued" },
  { ...SUBAGENTS[3], state: "not_started" },
];

const STARTED_CARD = {
  action_id: PID,
  kind: "session",
  title: "web-shop",
  agent: "Claude — API · Researcher v3",
  text: "Research an i18n library for web-shop and report the pick.",
  state: "started",
  engine: "claude-api",
  runtime: "api",
  bypass_default: false,
  bypass: false,
  session_key: K1,
  reason: "",
  notice: "",
  can_start: false,
  can_dismiss: false,
  decided_by: "hq",
  subagent: "sp-1",
};

async function conversation(page: Page, card: Record<string, unknown>, subagents: Record<string, unknown>[], live: Record<string, unknown>[]) {
  const fake = await shell(page, card);
  fake.conversations[0].turns[0].turn_id = TURN;
  fake.conversations[0].subagents = subagents;
  const calls: { path: string; body: unknown }[] = [];
  const state = { live: [...live], cancel: 200 };
  await page.route(/\/api\/ask\/conversations\/ask_[0-9a-f]{32}\/subagents$/, (r) =>
    r.fulfill({ json: { subagents: state.live } }),
  );
  await page.route(/\/subagents\/[^/]+\/sp-\d+\/cancel$/, (r) => {
    calls.push({ path: new URL(r.request().url()).pathname, body: null });
    if (state.cancel !== 200)
      return r.fulfill({ status: state.cancel, json: { detail: "that spawn is not waiting for a slot any more" } });
    state.live = state.live.map((x) => (x.handle === "sp-3" ? { ...x, outcome: "cancelled", state: "cancelled" } : x));
    return r.fulfill({ json: { handle: "sp-3", outcome: "cancelled" } });
  });
  await page.route(/\/api\/structured\/sessions\/[^/]+\/interrupt$/, (r) => {
    calls.push({ path: new URL(r.request().url()).pathname, body: r.request().postDataJSON() });
    return r.fulfill({ status: 202, json: {} });
  });
  await page.route(/\/api\/sessions\/[^/]+\/driver$/, (r) => {
    calls.push({ path: new URL(r.request().url()).pathname, body: r.request().postDataJSON() });
    return r.fulfill({ json: { who: "hq", generation: 4 } });
  });
  return { calls, state };
}

async function openGroup(page: Page, theme: string): Promise<Locator> {
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await setTheme(page, theme);
  const group = panel.getByTestId("ask-subagents");
  await expect(group).toBeVisible();
  return group;
}

for (const theme of ["dark", "light"] as const) {
  test(`the task's subagents are one group with the Commander's own controls · ${theme}`, async ({ page }, info) => {
    const { calls } = await conversation(page, STARTED_CARD, SUBAGENTS, LIVE);
    const group = await openGroup(page, theme);
    const rows = group.getByTestId("ask-subagent");
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0)).toHaveAttribute("data-state", "working");
    await expect(rows.nth(0)).toContainText("steered 2×");
    await expect(rows.nth(1)).toContainText("you took over");
    await expect(rows.nth(2)).toContainText("session limit 6 / 6");
    // A queued spawn past its deadline reads not started, with no control.
    await expect(rows.nth(3)).toHaveAttribute("data-state", "not_started");
    await expect(rows.nth(3)).toContainText("the session limit stayed full");
    await expect(rows.nth(3).getByRole("button")).toHaveCount(0);
    // The card says HQ started it as a subagent.
    await expect(page.getByTestId("ask-session-kicker")).toHaveText("Started by HQ · subagent");
    // 44px targets, and a focus ring reached by keyboard.
    const stop = rows.nth(0).getByTestId("ask-subagent-stop");
    for (const b of [rows.nth(0).getByTestId("ask-subagent-open"), stop, rows.nth(1).getByTestId("ask-subagent-hand-back"), rows.nth(2).getByTestId("ask-subagent-cancel")])
      await target(b);
    await rows.nth(0).getByTestId("ask-subagent-open").focus();
    await page.keyboard.press("Tab");
    await expect(stop).toBeFocused();
    expect(await stop.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
    await page.screenshot({ path: info.outputPath(`subagent-group-${theme}.png`) });
    // Stop is the session's own interrupt route, as the operator.
    await page.keyboard.press("Enter");
    await expect.poll(() => calls.find((c) => c.path.endsWith("/interrupt"))?.path).toContain("/api/structured/sessions/");
    const interrupt = calls.find((c) => c.path.endsWith("/interrupt"))!;
    expect((interrupt.body as { turn_id: string }).turn_id).toBe("t-1");
    // Hand back is A7's CAS route with the generation read.
    await rows.nth(1).getByTestId("ask-subagent-hand-back").click();
    await expect.poll(() => calls.find((c) => c.path.endsWith("/driver"))?.body).toEqual({ who: "hq", generation: 3 });
    // Cancel on the queued spawn: it reads cancelled.
    await rows.nth(2).getByTestId("ask-subagent-cancel").click();
    await expect(rows.nth(2)).toHaveAttribute("data-state", "cancelled");
    await expect(rows.nth(2).getByTestId("ask-subagent-cancel")).toHaveCount(0);
    await noSidewaysScroll(page);
  });
}

test("a refused Cancel says so and keeps the row", async ({ page }) => {
  const { state } = await conversation(page, STARTED_CARD, SUBAGENTS, LIVE);
  state.cancel = 409;
  const group = await openGroup(page, "dark");
  const queued = group.getByTestId("ask-subagent").nth(2);
  await queued.getByTestId("ask-subagent-cancel").click();
  await expect(group.getByTestId("ask-subagents-error")).toContainText("Not cancelled");
  await expect(queued).toHaveAttribute("data-state", "queued");
});

test("no subagents yet: no group is drawn", async ({ page }) => {
  await conversation(page, STARTED_CARD, [], []);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await pickConversation(panel);
  await expect(panel.getByTestId("ask-session")).toBeVisible();
  await expect(panel.getByTestId("ask-subagents")).toHaveCount(0);
});

for (const theme of ["dark", "light"] as const) {
  test(`a tainted spawn and an effect parked after sign-out are cards that say why · ${theme}`, async ({ page }, info) => {
    await conversation(
      page,
      {
        ...STARTED_CARD,
        state: "pending",
        decided_by: undefined,
        bypass: null,
        session_key: "",
        can_start: true,
        can_dismiss: true,
        reason: "HQ read text from a session or connector in this conversation, so this asks first",
        subagent: "sp-5",
      },
      [{ turn_id: TURN, handle: "sp-5", outcome: "asks", title: "web-shop", agent: "Codex — API · Tester v2", task_n: 2, reason: "HQ read text from a session or connector in this conversation, so this asks first" }],
      [{ turn_id: TURN, handle: "sp-5", outcome: "asks", state: "asks", title: "web-shop", agent: "Codex — API · Tester v2", task_n: 2, reason: "HQ read text from a session or connector in this conversation, so this asks first" }],
    );
    const group = await openGroup(page, theme);
    const card = page.getByTestId("ask-session");
    await expect(card).toHaveAttribute("data-subagent", "sp-5");
    await expect(card.getByTestId("ask-session-asks-first")).toContainText("asks first");
    await expect(group.getByTestId("ask-subagent")).toContainText("its card waits for you");
    await target(card.getByRole("button", { name: /Start/ }).first());
    await noSidewaysScroll(page);
    await page.screenshot({ path: info.outputPath(`subagent-tainted-${theme}.png`) });
  });
}

test("an effect parked after sign-out is the Commander's own signed-in card", async ({ page }) => {
  await conversation(
    page,
    { ...STARTED_CARD, state: "pending", decided_by: undefined, bypass: null, session_key: "", can_start: true, can_dismiss: true, reason: "the Commander signed out after this question began", subagent: "sp-6" },
    [{ turn_id: TURN, handle: "sp-6", outcome: "asks", title: "web-shop", agent: "Claude — API · Writer v1", task_n: 2 }],
    [{ turn_id: TURN, handle: "sp-6", outcome: "asks", state: "asks", title: "web-shop", agent: "Claude — API · Writer v1", task_n: 2, reason: "the Commander signed out after this question began" }],
  );
  const group = await openGroup(page, "dark");
  const card = page.getByTestId("ask-session");
  await expect(card.getByTestId("ask-session-asks-first")).toContainText("signed out");
  await expect(group.getByTestId("ask-subagent")).toContainText("signed out");
});
