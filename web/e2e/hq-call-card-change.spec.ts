/** Changing and dismissing an HQ card on a call (#2120), in a real browser on desktop and mobile.
 *
 *  The server is mocked (`callFixture.ts`). HQ proposed a project card, then — asked to change the
 *  name — the corrected card, which replaced the first. Pinned: the replaced card reads "Replaced"
 *  with "Nothing ran" and offers no controls; the corrected card's armed line says a dismiss is
 *  heard too; a spoken dismiss reads "Dismissed by voice" in a neutral stamp (nothing ran, so no
 *  success colour); and the controls stay at the 44px floor. */
import { expect, test, type Page } from "@playwright/test";

import { CID, FAKE_MIC, connect, mockShell, say } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

test.use(FAKE_MIC);

const OLD = "1".repeat(32);
const NEW = "2".repeat(32);

function projectCard(id: string, name: string, over: Record<string, unknown> = {}) {
  const state = (over.state as string | undefined) ?? "pending";
  return {
    action_id: id,
    kind: "operation",
    op: "project.rename",
    risk: "reversible",
    label: "Rename project",
    verb: "Rename",
    title: "Promo site",
    text: `Renames the project to “${name}”.`,
    needs: [],
    state,
    summary: "",
    reason: "",
    can_confirm: state === "pending",
    can_dismiss: state === "pending",
    ...over,
  };
}

async function withChangedCard(page: Page) {
  await page.route(/\/api\/ask\/conversations\/c{32}$/, (r) =>
    r.fulfill({
      json: {
        id: CID,
        title: "call",
        running: false,
        turns: [
          {
            turn_id: "t1",
            question: "Change the name. It's Game Place Promotions.",
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
            proposals: [
              projectCard(OLD, "Gameplays Promotions", { state: "dismissed", replaced_by: NEW }),
              projectCard(NEW, "Game Place Promotions", { replaces: OLD }),
            ],
          },
        ],
      },
    }),
  );
}

test("a replaced card says so, and a spoken dismiss reads as a dismiss", async ({ page }) => {
  const srv = await mockShell(page);
  await withChangedCard(page);
  const panel = await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });

  const old = page.locator(`[data-testid="call-card"][data-card="${OLD}"]`);
  const fresh = page.locator(`[data-testid="call-card"][data-card="${NEW}"]`);
  await expect(old).toContainText("Replaced");
  await expect(old).toContainText("HQ replaced it with a corrected card. Nothing ran.");
  await expect(old.getByRole("button", { name: /^Confirm/ })).toHaveCount(0);

  // The corrected card is armed: the line says a dismiss is heard too.
  say(srv, { t: "challenge", cards: [NEW], expires_s: 20 });
  await expect(fresh.getByTestId("call-card-say")).toHaveText(/^Say “confirm” · (20|19) s$/);
  await expect(fresh.getByTestId("call-card-voice")).toContainText(
    "Listening for “confirm”, “dismiss” or “cancel”",
  );
  for (const b of await fresh.getByRole("button").all()) {
    const box = await b.boundingBox();
    if (box) expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }

  // HQ heard "dismiss it": its own route dismissed the card.
  say(srv, { t: "challenge", cards: [] });
  say(srv, { t: "settled", card: NEW, ok: true, status: 200, dismissed: true });
  const voice = fresh.getByTestId("call-card-voice");
  await expect(voice).toHaveAttribute("data-voice", "settled");
  await expect(voice).toContainText("Dismissed by voice");
  await expect(voice).toHaveAttribute("data-tone", "neutral");
  await expect(voice.locator(".nj-stamp")).not.toHaveClass(/is-ok|is-down|is-warn/);
});
