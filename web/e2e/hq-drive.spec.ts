import { expect, type Page, test } from "@playwright/test";
import { open as openCard, shell } from "./askOperationShell";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

// Who drives an API session (#2397), in a real browser, desktop and phone, dark and light: the
// header chip in its three states (HQ driving, you, a mission) and the empty state of a session HQ
// never drove; typing in an HQ-driven session flips it to "You're driving"; Hand back to HQ (44px,
// focus ring) flips it back; a stale Hand back (409) reloads the state; HQ's turns say From HQ; the
// phone reflows without sideways scroll. Red on main: the pane has no driver chip or bar at all.

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;
const TITLE = "Web shop: Polish strings";
const CID = "ask_" + "b".repeat(32);
const now = Math.floor(Date.now() / 1000);

const ROW = {
  id: KEY,
  engine: "codex-api",
  uuid: ID,
  short_uuid: ID.slice(0, 8),
  cwd: "/home/u/web-shop",
  project: { kind: "project", id: "p1", name: "web-shop", color: "#ffb000" },
  last_mtime: now - 60,
  first_user_message: "",
  title: TITLE,
  sticky: false,
  archived: false,
  ai_summary: "",
  ai_recap: "",
  reviewed_at: null,
  review_excluded: false,
  has_draft: false,
};

type Driver = Record<string, unknown>;
const HQ: Driver = {
  who: "hq",
  generation: 1,
  since: now - 600,
  conversation: { id: CID, title: "Polish rollout" },
  can_hand_back: false,
};
const YOU: Driver = {
  who: "commander",
  generation: 2,
  since: now - 60,
  conversation: { id: CID, title: "Polish rollout" },
  can_hand_back: true,
};
const NEVER: Driver = { who: "commander", generation: 0, since: null, can_hand_back: false };
const MISSION: Driver = {
  who: "mission",
  generation: 0,
  mission: { id: "msn_" + "c".repeat(32), title: "Release 0.21" },
  can_hand_back: false,
};

function turn(id: string, text: string, reply: string, fromHq: boolean) {
  return {
    kind: "operator",
    turn_id: id,
    operation_id: id,
    state: "completed",
    text,
    reply,
    ts: now - 300,
    reply_ts: now - 290,
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    attachments: [],
    context: {},
    requires_authority: fromHq,
    tools: [],
    tools_truncated: false,
    usage: null,
    ...(fromHq ? { from_hq: true } : {}),
  };
}

interface State {
  driver: Driver;
  turns: ReturnType<typeof turn>[];
  revision: number;
  handBack: { status: number; then: Driver } | null;
  posted: Record<string, unknown>[];
}

async function mockApp(page: Page, driver: Driver): Promise<State> {
  const state: State = {
    driver,
    turns: [turn("11111111-1111-4111-8111-111111111111", "Run the i18n tests and fix every missing pl key.", "12 keys added; 2 tests still fail.", true)],
    revision: 3,
    handBack: null,
    posted: [],
  };
  const snapshot = () => ({
    session_key: KEY,
    revision: state.revision,
    event_cursor: state.revision,
    cwd: "/home/u/web-shop",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: null,
    turns: state.turns,
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
    bypass: false,
    driver: state.driver,
  });
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse/notifications**", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/projects**", (r) =>
    r.fulfill({ json: { projects: [{ id: "p1", name: "web-shop", color: "#ffb000", archived: false }] } }),
  );
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "none",
        projects_hidden: [],
        project_names: {},
        agent_defaults: { bypass: false },
      },
    }),
  );
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: { sessions: [ROW], next_offset: null, total: 1, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}$`), (r) => r.fulfill({ json: ROW }));
  await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${ID}/driver$`), (r) => {
    state.posted.push(r.request().postDataJSON() as Record<string, unknown>);
    const answer = state.handBack ?? { status: 200, then: { ...HQ, generation: 3 } };
    state.driver = answer.then;
    if (answer.status !== 200)
      return r.fulfill({ status: answer.status, json: { detail: "who drives this session changed" } });
    return r.fulfill({ json: state.driver });
  });
  const structured = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z-]+)?(\\?.*)?$`);
  await page.route(structured, (r) => {
    const sub = r.request().url().match(structured)?.[1] ?? "";
    if (sub === "/events")
      return r.fulfill({ json: { session_key: KEY, revision: state.revision, next_cursor: state.revision, events: [] } });
    if (sub === "/turns" && r.request().method() === "POST") {
      // The Commander typed: the server took the session over before the turn (#2397).
      const body = r.request().postDataJSON() as { operation_id: string; text: string };
      state.turns = [...state.turns, turn(body.operation_id, body.text, "on it", false)];
      state.revision += 1;
      if (state.driver.who === "hq") state.driver = { ...YOU, generation: 2 };
      return r.fulfill({ json: { session_key: KEY, revision: state.revision, ...state.turns.at(-1), handoff: "sent" } });
    }
    if (sub === "") return r.fulfill({ json: snapshot() });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  return state;
}

async function open(page: Page, theme: "dark" | "light") {
  await page.goto(`/s/codex-api/${ID}`);
  await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
}

async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

for (const theme of ["dark", "light"] as const) {
  test.describe(`${theme} theme`, () => {
    test("HQ driving: the chip names the conversation, the composer says typing takes over, HQ's turn says From HQ", async ({ page }) => {
      await mockApp(page, HQ);
      await open(page, theme);
      const chip = page.getByTestId("structured-driver");
      await expect(chip).toHaveAttribute("data-who", "hq");
      await expect(chip).toContainText("HQ driving");
      await expect(chip).toContainText("Polish rollout");
      await expect(page.getByTestId("structured-driver-bar")).toContainText("Typing below takes it over");
      await expect(page.getByRole("textbox", { name: /Message/ })).toHaveAttribute(
        "placeholder",
        /sending takes over from HQ/,
      );
      await expect(page.getByTestId("structured-from-hq").first()).toHaveText("From HQ");
      await expect(page.getByTestId("structured-hand-back")).toHaveCount(0);
      await noSidewaysScroll(page);
    });

    test("typing in an HQ-driven session flips the chip to You're driving, and Hand back flips it back", async ({ page }) => {
      const state = await mockApp(page, HQ);
      await open(page, theme);
      await expect(page.getByTestId("structured-driver")).toHaveAttribute("data-who", "hq");
      const box = page.getByRole("textbox", { name: /Message/ });
      await box.fill("I'll take it from here.");
      await box.press("Enter");
      await expect(page.getByTestId("structured-driver")).toHaveAttribute("data-who", "commander", { timeout: 15_000 });
      await expect(page.getByTestId("structured-driver")).toContainText("You’re driving");
      const back = page.getByTestId("structured-hand-back");
      await expect(back).toBeVisible();
      const rect = (await back.boundingBox())!;
      expect(rect.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
      await back.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(back).toBeFocused();
      const ring = await back.evaluate((el) => getComputedStyle(el).outlineStyle);
      expect(ring).not.toBe("none");
      await page.keyboard.press("Enter");
      await expect(page.getByTestId("structured-driver")).toHaveAttribute("data-who", "hq");
      expect(state.posted).toEqual([{ who: "hq", generation: 2 }]);
      await noSidewaysScroll(page);
    });

    test("a stale Hand back (409) says so and reloads who drives it", async ({ page }) => {
      const state = await mockApp(page, YOU);
      // Meanwhile another conversation of HQ's took it: the CAS fails, the view reloads.
      state.handBack = { status: 409, then: { ...HQ, generation: 4, conversation: { id: CID, title: "Release prep" } } };
      await open(page, theme);
      await page.getByTestId("structured-hand-back").click();
      await expect(page.getByTestId("structured-hand-back-error")).toContainText("changed meanwhile");
      await expect(page.getByTestId("structured-driver")).toHaveAttribute("data-who", "hq");
      await expect(page.getByTestId("structured-driver")).toContainText("Release prep");
    });

    test("a mission holds it: the chip names the mission, and nothing is offered", async ({ page }) => {
      await mockApp(page, MISSION);
      await open(page, theme);
      const chip = page.getByTestId("structured-driver");
      await expect(chip).toHaveAttribute("data-who", "mission");
      await expect(chip).toContainText("Mission · Release 0.21");
      await expect(page.getByTestId("structured-driver-bar")).toContainText("A mission holds this session");
      await expect(page.getByTestId("structured-hand-back")).toHaveCount(0);
      await noSidewaysScroll(page);
    });

    test("a session HQ never drove offers no Hand back: Ask HQ to take it over", async ({ page }) => {
      await mockApp(page, NEVER);
      await open(page, theme);
      await expect(page.getByTestId("structured-driver")).toHaveAttribute("data-who", "commander");
      await expect(page.getByTestId("structured-driver-bar")).toContainText("Ask HQ to take it over");
      await expect(page.getByTestId("structured-hand-back")).toHaveCount(0);
      await noSidewaysScroll(page);
    });
  });
}

// The HQ conversation (#2397): a send HQ made itself reads Sent by HQ, and the take-over notice
// is the session's row in the conversation's Following block.

for (const theme of ["dark", "light"] as const) {
  test(`the conversation says Sent by HQ, and You took over once the Commander typed · ${theme}`, async ({ page }) => {
    const fake = await shell(page, {
      action_id: "a".repeat(32),
      kind: "operation",
      op: "session.send",
      risk: "external",
      label: "Send to session",
      verb: "Send",
      title: TITLE,
      text: "Sends this as the session's next message: “Run the i18n tests and fix every missing pl key.”",
      needs: [],
      state: "done",
      summary: "Sent into the session.",
      reason: "",
      can_confirm: false,
      can_dismiss: false,
      decided_by: "hq",
    });
    fake.conversations[0].following = [KEY];
    fake.conversations[0].updates = [
      { key: KEY, kind: "taken_over", title: TITLE, ts: now, following: true },
    ];
    const c = await openCard(page);
    await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
    await expect(c.getByTestId("ask-operation-kicker")).toHaveText("Sent by HQ");
    const row = page.getByTestId("ask-follow-row").first();
    await expect(row).toHaveAttribute("data-kind", "taken_over");
    await expect(row).toContainText("You took over");
    await expect(row).toContainText("HQ stopped steering it");
    await noSidewaysScroll(page);
  });
}
