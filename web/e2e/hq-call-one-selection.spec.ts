/** One open selection on a call (#1911), in a real browser on desktop and mobile: a newer turn's
 *  card or open question takes the older still-pending card off the call screen, and a turn that
 *  presents nothing new leaves it there. The server is mocked (`callFixture.ts`). */
import { expect, test, type Page } from "@playwright/test";

import { FAKE_MIC, connect, mockShell, say } from "./callFixture";

test.use(FAKE_MIC);

const nudge = (id: string) => ({
  action_id: id,
  kind: "nudge",
  key: "claude:00000000-0000-4000-8000-000000000001",
  title: `card ${id}`,
  agent: "claude",
  text: "Rerun the auth tests.",
  state: "pending",
  projection: "actionable",
  can_approve: true,
  can_reject: true,
  detail: "",
});

const turn = (id: string, extra: Record<string, unknown>) => ({
  turn_id: id,
  question: "…",
  asked_at: 1,
  status: "done",
  answer: "Here it is.",
  detail: null,
  code: null,
  matches: [],
  mission_matches: [],
  tools: [],
  ask: null,
  contact: "asia-woman",
  proposals: [],
  ...extra,
});

async function onCall(page: Page, turns: { current: unknown[] }) {
  const srv = await mockShell(page);
  await page.route(/\/api\/ask\/conversations\/c{32}$/, (r) =>
    r.fulfill({ json: { id: "c".repeat(32), title: "call", running: false, turns: turns.current } }),
  );
  const panel = await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  return srv;
}

const shown = (page: Page) => page.getByTestId("call-card");

test("a reworked card replaces the old one on the call screen; talk alone keeps it (#1911)", async ({ page }) => {
  const turns = { current: [turn("t1", { proposals: [nudge("old")] })] };
  const srv = await onCall(page, turns);
  say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });
  await expect(shown(page)).toHaveCount(1);
  await expect(shown(page).first()).toHaveAttribute("data-card", "old");

  // Small talk: nothing new presented, the open card stays.
  turns.current = [...turns.current, turn("t2", {})];
  say(srv, { t: "turn", turn: { turn_id: "t2", status: "done" } });
  await expect(shown(page)).toHaveCount(1);
  await expect(shown(page).first()).toHaveAttribute("data-card", "old");

  // HQ reworks the card: only the new one is on the call screen.
  turns.current = [...turns.current, turn("t3", { proposals: [nudge("new")] })];
  say(srv, { t: "turn", turn: { turn_id: "t3", status: "done" } });
  await expect(page.locator('[data-testid="call-card"][data-card="new"]')).toBeVisible();
  await expect(shown(page)).toHaveCount(1);
});

test("HQ's newer question takes the older card off the call screen (#1911)", async ({ page }) => {
  const turns = { current: [turn("t1", { proposals: [nudge("old")] })] };
  const srv = await onCall(page, turns);
  say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });
  await expect(shown(page)).toHaveCount(1);
  turns.current = [
    ...turns.current,
    turn("t2", {
      answer: "Which project?",
      ask: { question_id: "q1", question: "Which project?", options: ["Battlelab", "Docs Site"], allow_text: false, answered: false },
    }),
  ];
  say(srv, { t: "turn", turn: { turn_id: "t2", status: "done" } });
  await expect(shown(page)).toHaveCount(0);
});
