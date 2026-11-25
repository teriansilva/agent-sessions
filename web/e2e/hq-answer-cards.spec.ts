/** What sits under an HQ answer, in a real browser, desktop and mobile.
 *
 *  - Session rows are what the Commander ASKED HQ to find. A session HQ only looked up for its own
 *    work (stored `hidden`) is never a row, so a turn about something else carries no "Open /
 *    Open in map" list of unrelated sessions.
 *  - A turn that drew the steps AND asks a question shows the plan first and the question last,
 *    right above the composer: the question is what the Commander answers next, and it was
 *    drawn above the steps it is about.
 */
import { expect, test, type Page } from "@playwright/test";

import { shell } from "./askOperationShell";
import { openAsk, pickConversation } from "./askSidebar";
import type { FakeAsk } from "./askStream";

const ASKED = "claude:11111111-1111-4111-8111-111111111111";
const LOOKED_UP = "claude:22222222-2222-4222-8222-222222222222";
const LIST = "c".repeat(16);

async function setup(page: Page, edit: (fake: FakeAsk) => void): Promise<void> {
  const fake = await shell(page, {
    action_id: "a".repeat(32),
    kind: "nudge",
    key: ASKED,
    title: "",
    text: "hello",
    state: "dismissed",
    can_confirm: false,
    can_dismiss: false,
  });
  fake.conversations[0].turns[0].proposals = [];
  edit(fake);
}

async function open(page: Page): Promise<void> {
  await page.goto("/dashboard");
  await pickConversation(await openAsk(page));
}

test("a session HQ only looked up is never a row under the answer", async ({ page }) => {
  await setup(page, (fake) => {
    const t = fake.conversations[0].turns[0];
    t.answer = "Two things are missing: which folder, and how the automation reaches it.";
    t.matches = [
      { id: ASKED, title: "Refresh the listing inbox", why: "it refreshed the listing overview" },
      { id: LOOKED_UP, title: "Deploy the review fixes", why: "", hidden: true },
    ];
  });
  await open(page);
  await expect(page.getByTestId("ask-answer")).toBeVisible();
  const rows = page.getByTestId("ask-match");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("Refresh the listing inbox");
  await expect(page.getByText("Deploy the review fixes")).toHaveCount(0);
  // One row is not a batch: no "Open all in map".
  await expect(page.getByTestId("ask-match-map-all")).toHaveCount(0);
});

test("an answer with only looked-up sessions shows no session rows at all", async ({ page }) => {
  await setup(page, (fake) => {
    const t = fake.conversations[0].turns[0];
    t.answer = "HQ cannot start that session itself. How should it be made?";
    t.matches = [
      { id: ASKED, title: "Refresh the listing inbox", why: "", hidden: true },
      { id: LOOKED_UP, title: "Deploy the review fixes", why: "", hidden: true },
    ];
  });
  await open(page);
  await expect(page.getByTestId("ask-answer")).toBeVisible();
  await expect(page.getByTestId("ask-match")).toHaveCount(0);
  await expect(page.getByText("Refresh the listing inbox")).toHaveCount(0);
});

test("the question comes after the steps the same turn drew", async ({ page }) => {
  await setup(page, (fake) => {
    const conv = fake.conversations[0];
    const t = conv.turns[0];
    t.answer = "HQ cannot start that session itself. How should it be made?";
    t.ask = {
      question_id: "q1",
      question: "How should the fixed session be made?",
      options: ["I start it myself", "Use Claude Code"],
      allow_text: true,
      answered: false,
    };
    const step = (n: number, title: string, state: string) => ({
      sid: `s0000000${n}`,
      n,
      title,
      detail: "",
      needs: n > 1 ? [`s0000000${n - 1}`] : [],
      state,
      reason: "",
      outcome: "",
      cards: [],
      settled_at: null,
    });
    conv.step_lists = [
      {
        id: LIST,
        revision: 1,
        title: "Listing overview: fixed session + automation",
        status: "active",
        current: "s00000001",
        drawn_at_turn: t.turn_id,
        created_turn: t.turn_id,
        owed: false,
        steps: [
          step(1, "Start the fixed session", "now"),
          step(2, "Draft the automation, switched off", "next"),
        ],
      },
    ];
  });
  await open(page);
  const steps = page.getByTestId("ask-steps");
  const question = page.getByTestId("ask-question");
  await expect(steps).toBeVisible();
  await expect(question).toBeVisible();
  await expect(question).toHaveCount(1);
  const s = await steps.boundingBox();
  const q = await question.boundingBox();
  expect(s && q).toBeTruthy();
  // On screen, top to bottom: the whole step list, then the question.
  expect(q!.y).toBeGreaterThanOrEqual(s!.y + s!.height);
});
