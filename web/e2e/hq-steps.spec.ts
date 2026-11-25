/** HQ's step list (#2251), in a real browser, desktop and mobile.
 *
 *  What a DOM emulator cannot prove: that exactly ONE card is on screen (the current step's, in
 *  its slot, never again in its turn); that a card HQ replaced is gone and the card that took its
 *  place shows what changed; that the list is drawn once, under the newest turn that changed it,
 *  with a quiet line where it was; that "Discuss this step" works by keyboard and keeps a draft
 *  already in the box; that End steps posts the list id; that the controls meet the 44px floor on
 *  a phone and nothing scrolls sideways.
 */
import { expect, test, type Page } from "@playwright/test";

import { CID, minTarget, noSidewaysScroll, open, PID, shell } from "./askOperationShell";
import { turnOf, type FakeAsk } from "./askStream";

const OLD = "b".repeat(32);
const LIST = "c".repeat(16);

function card(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "project.create",
    risk: "reversible",
    label: "Create project",
    verb: "Create project",
    title: "",
    text: "Creates the project “Rentals” in the folder you choose below.",
    needs: ["folder"],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    ...over,
  };
}

function steps(turn: string, created: string, over: Record<string, unknown> = {}) {
  const step = (n: number, sid: string, title: string, state: string, cards: string[] = []) => ({
    sid,
    n,
    title,
    detail: n === 2 ? "Eine Claude-Code-Session im neuen Projekt legt das Repo an." : "",
    needs: n === 2 ? ["s00000001"] : [],
    state,
    reason: "",
    outcome: "",
    cards,
    settled_at: null,
  });
  return {
    id: LIST,
    revision: 2,
    title: "Set up Rentals",
    status: "active",
    current: "s00000001",
    drawn_at_turn: turn,
    created_turn: created,
    owed: false,
    steps: [
      step(1, "s00000001", "Projekt „Rentals“ anlegen", "now", [PID]),
      step(2, "s00000002", "Forgejo-Repo „Rentals“ anlegen und auschecken", "next"),
      step(3, "s00000003", "Playbook „Rentals“", "next"),
    ],
    ...over,
  };
}

/** Two turns: the first staged "Housing" (since replaced), the second the corrected card. */
async function setup(page: Page): Promise<FakeAsk> {
  const fake = await shell(page, card());
  const conv = fake.conversations[0];
  const first = conv.turns[0];
  first.question = "Erstelle ein neues Projekt Housing, ein Forgejo-Repo, ein Playbook";
  first.answer = "Drei Schritte; vor dir liegt Schritt 1.";
  first.proposals = [
    card({
      action_id: OLD,
      text: "Creates the project “Housing” in the folder you choose below.",
      state: "dismissed",
      replaced_by: PID,
      can_confirm: false,
      can_dismiss: false,
    }),
  ];
  const second = turnOf("Nenne das Projekt besser Rentals", {
    answer: "Erledigt: das Projekt heißt jetzt „Rentals“.",
  });
  second.proposals = [
    card({ replaces: OLD, changes: [{ field: "name", from: "Housing", to: "Rentals" }] }),
  ];
  conv.turns.push(second);
  conv.step_lists = [steps(second.turn_id, first.turn_id)];
  return fake;
}

test("one card on screen: the current step's, in its slot; the replaced card is gone", async ({
  page,
}) => {
  await setup(page);
  await open(page);
  const list = page.getByTestId("ask-steps");
  await expect(list).toBeVisible();
  await expect(page.getByTestId("ask-steps")).toHaveCount(1);
  // Exactly one card, and it is in the step's slot.
  await expect(page.getByTestId("ask-operation")).toHaveCount(1);
  await expect(list.getByTestId("ask-step-slot").getByTestId("ask-operation")).toHaveCount(1);
  await expect(page.getByText("Creates the project “Housing”")).toHaveCount(0);
  // The card that replaced it says what changed.
  const changed = list.getByTestId("ask-card-changed");
  await expect(changed).toContainText("Name");
  await expect(changed.locator("s")).toHaveText("Housing");
  await expect(changed).toContainText("Rentals");
  // The rows: now, then two upcoming steps with their Discuss buttons.
  await expect(list.getByTestId("ask-step")).toHaveCount(3);
  await expect(list.getByTestId("ask-steps-progress")).toContainText("Step 1 of 3");
  await expect(list.getByTestId("ask-step-discuss")).toHaveCount(2);
  await expect(list.getByText("Needs step 1")).toBeVisible();
  // Drawn once, under the newest turn; the first turn keeps one quiet line.
  const turns = page.getByTestId("ask-turn");
  await expect(turns.nth(1).getByTestId("ask-steps")).toHaveCount(1);
  await expect(turns.nth(0).getByTestId("ask-steps")).toHaveCount(0);
  await expect(turns.nth(0).getByTestId("ask-steps-moved")).toBeVisible();
  await noSidewaysScroll(page);
});

test("Discuss this step by keyboard puts the step in front of the draft", async ({ page }) => {
  await setup(page);
  await open(page);
  const box = page.getByTestId("ask-sidebar").locator("textarea").first();
  await box.fill("bitte privat");
  const discuss = page.getByTestId("ask-step-discuss").first();
  await discuss.focus();
  await page.keyboard.press("Enter");
  await expect(box).toHaveValue("Step 2: bitte privat");
  await expect(box).toBeFocused();
  // Again: the prefix is not doubled.
  await discuss.focus();
  await page.keyboard.press("Enter");
  await expect(box).toHaveValue("Step 2: bitte privat");
});

test("End steps posts the list id and the list reads ended", async ({ page }) => {
  const fake = await setup(page);
  const bodies: unknown[] = [];
  await page.route(`**/api/ask/conversations/${CID}/steps/end`, (r) => {
    bodies.push(r.request().postDataJSON());
    const conv = fake.conversations[0];
    const lst = conv.step_lists![0] as Record<string, unknown>;
    lst.status = "ended";
    lst.current = null;
    lst.steps = (lst.steps as Record<string, unknown>[]).map((x) => ({ ...x, state: "ended" }));
    const p = conv.turns[1].proposals![0];
    Object.assign(p, { state: "dismissed", can_confirm: false, can_dismiss: false });
    return r.fulfill({ json: { steps: lst } });
  });
  await open(page);
  await page.getByTestId("ask-steps-end").click();
  await expect(page.getByTestId("ask-steps")).toHaveAttribute("data-status", "ended");
  expect(bodies).toEqual([{ list_id: LIST }]);
  await expect(page.getByTestId("ask-steps-progress")).toContainText("Ended");
  await expect(page.getByTestId("ask-steps-end")).toHaveCount(0);
  await expect(page.getByTestId("ask-operation")).toHaveCount(0);
});

test("the step controls meet the touch floor", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "the 44px floor is the phone's");
  await setup(page);
  await open(page);
  await minTarget(page.getByTestId("ask-step-discuss").first());
  await minTarget(page.getByTestId("ask-steps-end"));
  await noSidewaysScroll(page);
});
