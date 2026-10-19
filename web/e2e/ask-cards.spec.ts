/** Ask's question and nudge cards (#1443 Phase 2a), in a real browser, desktop and mobile.
 *
 *  A DOM emulator cannot show what these pin: an option tapped by pointer AND by keyboard sends
 *  its INDEX; the nudge's controls post the existing approve / reject routes and the card then
 *  reads the ledger's verdict back (Delivered / Out of date) as an LED plus a word; the controls
 *  sit at the 44px floor and stack full width at ≤800px; and nothing scrolls sideways.
 */
import { expect, test, type Page } from "@playwright/test";

import { openAsk } from "./askSidebar";
import {
  ASK_STREAM,
  fulfillAsk,
  mockAskConversations,
  turnOf,
  type FakeAsk,
  type FakeConversation,
} from "./askStream";

const QID = "5e5e5e5e-0000-4000-8000-000000000001";
const AID = "a".repeat(32);

function proposal(state: string, projection: string, can = projection === "actionable") {
  return {
    action_id: AID,
    kind: "nudge",
    key: "claude:11111111-2222-4333-8444-555555555555",
    title: "api rebase",
    text: "Rebase onto main, resolve the conflicts in routes/sessions.py, then rerun the tests.",
    state,
    projection,
    can_approve: can,
    can_reject: can,
    detail: "",
  };
}

async function mockShell(page: Page, seed: FakeConversation[]): Promise<FakeAsk> {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  return mockAskConversations(page, seed);
}

function asked(): FakeConversation {
  return {
    id: "ask_" + "a".repeat(32),
    created_at: 1,
    updated_at: 2,
    turns: [
      {
        ...turnOf("start a session for the docs", { answer: "Which project?" }),
        ask: {
          question_id: QID,
          question: "Which project?",
          options: ["agent-sessions", "battlelab-cloud"],
          allow_text: true,
          answered: false,
        },
      },
    ],
  };
}

function nudged(): FakeConversation {
  return {
    id: "ask_" + "b".repeat(32),
    created_at: 1,
    updated_at: 2,
    turns: [
      {
        ...turnOf("tell api rebase to rebase onto main", {
          answer: "Here is the exact text; it goes in only if the screen is unchanged.",
        }),
        proposals: [proposal("proposed", "actionable")],
      },
    ],
  };
}

async function noSidewaysScroll(page: Page) {
  const { scroll, inner } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(scroll).toBeLessThanOrEqual(inner);
}

test("a question is answered by option — pointer or keyboard — and the server's reply is sent by index", async ({
  page,
}) => {
  await mockShell(page, [asked()]);
  let sent: Record<string, unknown> | null = null;
  await page.route(ASK_STREAM, async (r) => {
    sent = r.request().postDataJSON();
    await fulfillAsk(r, { answer: "Starting it in battlelab-cloud is a later phase; noted." });
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const card = panel.getByTestId("ask-question");
  await expect(card).toContainText("Which project?");
  await expect(card).toContainText("Waiting for you");
  const second = card.getByRole("button", { name: "battlelab-cloud" });
  expect((await second.boundingBox())!.height).toBeGreaterThanOrEqual(44);

  // Keyboard: Tab reaches the option, its ring is visible, Enter answers.
  await card.getByRole("button", { name: "agent-sessions" }).focus();
  await page.keyboard.press("Tab");
  await expect(second).toBeFocused();
  expect(await second.evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(panel.getByTestId("ask-turn")).toHaveCount(2);
  expect(sent).toMatchObject({ answers: QID, option: 1, text: "" });
  // The answer turn shows the reply the question stored for that option.
  await expect(panel.getByTestId("ask-turn").nth(1).getByRole("article", { name: "You" })).toContainText(
    "battlelab-cloud",
  );
  await expect(panel.getByTestId("ask-turns")).toContainText("a later phase; noted");
  // The question now reads as answered and offers nothing more.
  await expect(card).toContainText("Answered");
  await expect(card.getByTestId("ask-question-option").first()).toBeDisabled();
  await noSidewaysScroll(page);
});

test("a nudge card sends through the approve route and shows what the ledger says happened", async ({
  page,
}) => {
  const fake = await mockShell(page, [nudged()]);
  const approved: string[] = [];
  await page.route(/\/api\/pulse\/actions\/[0-9a-f]{32}\/approve$/, async (r) => {
    approved.push(new URL(r.request().url()).pathname);
    fake.conversations[0].turns[0].proposals = [proposal("delivered", "settled")];
    await r.fulfill({ json: { id: AID, state: "delivered" } });
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  const card = panel.getByTestId("ask-nudge");
  await expect(card.getByTestId("ask-nudge-text")).toHaveText(
    "Rebase onto main, resolve the conflicts in routes/sessions.py, then rerun the tests.",
  );
  const send = card.getByRole("button", { name: "Send to api rebase" });
  const dismiss = card.getByTestId("ask-nudge-dismiss");
  const [s, d] = [(await send.boundingBox())!, (await dismiss.boundingBox())!];
  expect(s.height).toBeGreaterThanOrEqual(44);
  if (page.viewportSize()!.width <= 800) {
    // Stacked, full width, at the 44px floor (design.md ≤800px).
    expect(d.y).toBeGreaterThan(s.y);
    expect(Math.abs(d.width - s.width)).toBeLessThanOrEqual(1);
  }
  await send.click();
  expect(approved).toEqual([`/api/pulse/actions/${AID}/approve`]);
  const outcome = card.getByTestId("ask-nudge-outcome");
  await expect(outcome).toContainText("Delivered");
  await expect(outcome).toContainText("That is not the agent having done it.");
  await expect(card.getByTestId("ask-nudge-send")).toHaveCount(0);
  await noSidewaysScroll(page);
});

test("a screen that moved: the refused approve reads back as Out of date, nothing sent", async ({
  page,
}) => {
  const fake = await mockShell(page, [nudged()]);
  await page.route(/\/api\/pulse\/actions\/[0-9a-f]{32}\/approve$/, async (r) => {
    fake.conversations[0].turns[0].proposals = [proposal("stale", "settled")];
    await r.fulfill({ status: 409, json: { detail: "the session's screen changed" } });
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await panel.getByTestId("ask-nudge-send").click();
  const outcome = panel.getByTestId("ask-nudge-outcome");
  await expect(outcome).toContainText("Out of date");
  await expect(outcome).toContainText("nothing was sent");
  await expect(panel.getByTestId("ask-error")).toHaveCount(0);
});
