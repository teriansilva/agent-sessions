/** API members on a mission (#1900), in a real browser.
 *
 *  A mission holding one console session and one API session. The API member has no terminal
 *  screen, so the console must not draw one for it: its Now row is tagged API and links to its
 *  conversation, its Context block says why there is no screen and links there too, and its
 *  waiting requests arrive as ONE decision card with a count, up to three requests and the same
 *  link, with nothing to approve. On the phone every one of those controls is a 44px target.
 *
 *  Network fully mocked; the suite never talks to a backend.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  MISSION,
  missionList,
  missionRow,
  mockMissions,
  openMissionConversation,
  openMissionDetails,
} from "./mission-console";
import { mockRoster } from "./roster";
import { TOUCH_FLOOR } from "./touchTarget";

const NOW = Math.floor(Date.now() / 1000);
const CONSOLE_KEY = "claude:aaaaaaaa-0000-4000-8000-000000001900";
const API_UUID = "8c41aaaa-0000-4000-8000-000000001900";
const API_KEY = `claude-api:${API_UUID}`;
const PANE = `/s/claude-api/${API_UUID}`;
/** A server-shaped mission id (`msn_` + 32 hex), so the `?m=` deep link is honoured. */
const MID = `msn_${"1900".repeat(8)}`;
const RATIONALE =
  "7 approvals waiting in Review auth changes. Decide each one in its conversation.";

const API_CARD = {
  id: "act-api-pending",
  state: "escalated",
  projection: "actionable",
  can_approve: false,
  can_reject: true,
  ts: NOW - 60,
  tier: "suggest",
  session_id: API_KEY,
  engine: "claude-api",
  title: "Review auth changes",
  project: "agent-sessions",
  project_id: "p1",
  verb: "escalate",
  confidence: 1,
  rationale: RATIONALE,
  evidence: "none",
  escalation_reason: "api_pending",
  api_pending: {
    count: 7,
    fp: ["r1", "r2", "r3", "r4", "r5", "r6", "r7"],
    pane: PANE,
    items: [
      { request_id: "r1", kind: "Edit", label: "src/auth/session.py", question: false },
      { request_id: "r2", kind: "Bash", label: "npm test -- login", question: false },
      {
        request_id: "r3",
        kind: "question",
        label: "Keep the old cookie name for one release?",
        question: true,
      },
    ],
  },
};

function card(key: string, engine: string, action: unknown | null) {
  return {
    id: key,
    engine,
    title: action ? "Review auth changes" : "Harden the login flow",
    cwd: "/home/u/agent-sessions",
    project: { kind: "project", id: "p1", name: "agent-sessions", color: "#ffb000" },
    state: action ? "needs_you" : "idle",
    live: false,
    last_activity: NOW - 600,
    last_mtime: NOW - 600,
    intervention_required: false,
    ai_summary: "",
    synthesis: "",
    mission_id: MID,
    ...(action ? { pending_action: action } : {}),
  };
}

async function openConsole(page: Page, { roster = true } = {}) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        pulse: { configured: true },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0 } }),
  );
  await page.route(/\/api\/pulse\/orchestrator$/, (r) =>
    r.fulfill({
      json: {
        config: { enabled: true },
        pending: [API_CARD],
        feed: [],
        expired_now: 0,
        delivering_verbs: ["continue", "choose", "answer"],
        running: [],
        last: {},
      },
    }),
  );
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({
      json: {
        cache_version: 2,
        generated_at: NOW,
        window_days: 3,
        scan_depth: "slow",
        input_fingerprint: null,
        synthesis_skipped: false,
        banner: null,
        cards: [card(CONSOLE_KEY, "claude", null), card(API_KEY, "claude-api", API_CARD)],
      },
    }),
  );
  const keys = [CONSOLE_KEY, API_KEY];
  const sessions = keys.map((k) => ({ session_key: k, removed_at: null }));
  await mockMissions(page, {
    missions: missionList([missionRow({ id: MID, title: "Harden the login flow", session_keys: keys })]),
    mission: {
      ...MISSION,
      id: MID,
      title: "Harden the login flow",
      sessions,
      events: [],
      events_next_seq: null,
    },
    context: { id: MID, project_id: "", cwd: "/repo", sessions, git: null, git_error: null },
    now: {
      checked_at: NOW,
      sessions: [
        {
          session_key: CONSOLE_KEY,
          status: "producing",
          seconds_since_output: 4,
          prompt_class: null,
          recap_age_s: null,
          recap_older_than_output: false,
        },
        {
          session_key: API_KEY,
          status: "at_prompt",
          seconds_since_output: 40,
          prompt_class: "approval",
          recap_age_s: null,
          recap_older_than_output: false,
          api: true,
          pending_count: 7,
        },
      ],
    },
  });
  if (roster) {
    await mockRoster(page); // the manifest-generated roster: `claude-api` runs over the API
  } else {
    // A page whose roster has not loaded, in a browser that never saw one.
    await page.route("**/api/engines", (r) => r.fulfill({ status: 503, json: { detail: "down" } }));
    await page.addInitScript(() => localStorage.removeItem("battlelab.engineRuntimes.v1"));
  }
  await page.goto(`/mission?m=${MID}`);
  await expect(page.getByTestId("console-title")).toHaveText("Harden the login flow");
}

async function height(l: Locator): Promise<number> {
  return l.evaluate((el) => el.getBoundingClientRect().height);
}

/** The keyboard focus ring (Hermes on #1900): focused from the keyboard, the control draws the
 *  shared plate's INSET outline — solid, at least 2px, a negative offset. */
async function insetRing(l: Locator): Promise<boolean> {
  await l.focus();
  await l.page().keyboard.press("Shift+Tab");
  await l.page().keyboard.press("Tab");
  return l.evaluate((el) => {
    const cs = getComputedStyle(el);
    return (
      document.activeElement === el &&
      cs.outlineStyle === "solid" &&
      parseFloat(cs.outlineWidth) >= 2 &&
      parseFloat(cs.outlineOffset) < 0
    );
  });
}

test("an API member shows its conversation, never an empty terminal (#1900)", async ({
  page,
}, testInfo) => {
  await openConsole(page);
  const phone = testInfo.project.name === "mobile";
  const touch: Locator[] = [];

  // NOW STRIP: the API row is tagged and links to the conversation; the console row is not.
  const rows = page.locator('[data-testid="mission-now"]:visible').getByTestId("mission-now-row");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).getByTestId("mission-now-api")).toHaveCount(0);
  await expect(rows.nth(0).getByRole("link")).toHaveCount(0);
  const apiRow = rows.nth(1);
  await expect(apiRow.getByTestId("mission-now-api")).toHaveText("API");
  await expect(apiRow).toContainText("waiting on 7 approvals · last turn 40s ago");
  const stripLink = apiRow.getByRole("link", { name: /conversation/i });
  await expect(stripLink).toBeVisible();
  await expect(stripLink).toHaveAttribute("href", PANE);
  touch.push(stripLink);

  // THE CARD: a count, three requests, "and 4 more", the conversation, Dismiss and no Approve.
  const apiCard = page.locator('[data-testid="api-pending-card"]:visible');
  await expect(apiCard).toHaveCount(1);
  await expect(apiCard.getByTestId("api-pending-count")).toHaveText("7 waiting");
  await expect(apiCard).toContainText(RATIONALE);
  const items = apiCard.getByTestId("api-pending-items").locator("li");
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toContainText("EDIT");
  await expect(items.nth(0)).toContainText("src/auth/session.py");
  await expect(items.nth(1)).toContainText("BASH");
  await expect(items.nth(2)).toContainText("QUESTION");
  await expect(apiCard.getByTestId("api-pending-more")).toHaveText("and 4 more");
  const cardLink = apiCard.getByRole("link", { name: /open the conversation/i });
  await expect(cardLink).toBeVisible();
  await expect(cardLink).toHaveAttribute("href", PANE);
  await expect(apiCard.getByRole("button", { name: /approve/i })).toHaveCount(0);
  const dismiss = apiCard.getByRole("button", { name: /dismiss this escalation/i });
  await expect(dismiss).toBeVisible();
  touch.push(cardLink, dismiss);

  if (phone) {
    for (const l of touch) expect(await height(l)).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  // The strip's link and the card's link are plates: each draws the inset focus ring.
  for (const l of [stripLink, cardLink]) expect(await insetRing(l)).toBe(true);

  // CONTEXT: the API member's block has no VIEW SCREEN and no terminal frame, only its link.
  await openMissionDetails(page, "context");
  const apiBlock = page.locator(`[data-testid="mission-screen"][data-session="${API_KEY}"]:visible`);
  const consoleBlock = page.locator(
    `[data-testid="mission-screen"][data-session="${CONSOLE_KEY}"]:visible`,
  );
  await expect(apiBlock).toHaveCount(1);
  await expect(consoleBlock.getByTestId("view-screen")).toBeVisible();
  await expect(apiBlock.getByTestId("view-screen")).toHaveCount(0);
  await expect(apiBlock.locator("pre")).toHaveCount(0);
  await expect(apiBlock).toContainText("API session — it has no terminal screen.");
  const blockLink = apiBlock.getByRole("link", { name: /open the conversation/i });
  await blockLink.scrollIntoViewIfNeeded();
  await expect(blockLink).toBeVisible();
  await expect(blockLink).toHaveAttribute("href", PANE);
  // The operator's own relay to an API session still works server-side (#1899): the form stays.
  await expect(apiBlock.getByTestId("relay-input")).toBeVisible();
  if (phone) expect(await height(blockLink)).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(await insetRing(blockLink)).toBe(true);

  // The page never scrolls sideways.
  await openMissionConversation(page);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("before the roster lands, the server's {api: true} answer to VIEW SCREEN switches the block (#1900)", async ({
  page,
}) => {
  await openConsole(page, { roster: false });
  // After `mockMissions`' catch-all, so it answers ahead of it; read only on VIEW SCREEN.
  await page.route("**/api/missions/*/screen/**", (r) =>
    r.fulfill({ json: { api: true, session_key: API_KEY, pane: PANE } }),
  );
  await openMissionDetails(page, "context");
  const apiBlock = page.locator(`[data-testid="mission-screen"][data-session="${API_KEY}"]:visible`);
  await apiBlock.getByTestId("view-screen").click();
  await expect(apiBlock.getByTestId("screen-api")).toBeVisible();
  await expect(apiBlock.getByTestId("view-screen")).toHaveCount(0);
  await expect(apiBlock.locator("pre")).toHaveCount(0);
  await expect(apiBlock.getByRole("link", { name: /open the conversation/i })).toHaveAttribute(
    "href",
    PANE,
  );
});
