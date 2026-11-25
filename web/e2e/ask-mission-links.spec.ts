/** HQ links the missions it names (#1950), in a real browser, desktop and mobile.
 *
 *  The second live acceptance run: the draft-mission card read "Draft mission created" with no way
 *  into the mission, and HQ stopped after the tap. Here, the confirmed card's done line carries
 *  Open mission, HQ's continuation turn renders as a slim "After your confirm" line (never a "You"
 *  bubble, it is not the Commander's words), its team card links the mission while still pending,
 *  and the answer's Missions row links it too. Every link is the client's `missionLink` of the
 *  server's id; the spec checks the href, the 44px floor, a real navigation and no sideways scroll.
 */
import { expect, test } from "@playwright/test";

import { MISSION_PATH, missionLink } from "../src/lib/missionLink";
import { decideRoute, minTarget, noSidewaysScroll, open, PID, shell } from "./askOperationShell";
import { turnOf } from "./askStream";

const MID = "msn_" + "b".repeat(32);
const TEAM = "c".repeat(32);

function draftCard(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action_id: PID,
    kind: "operation",
    op: "mission.create",
    risk: "reversible",
    label: "Create a draft mission",
    verb: "Create draft",
    title: "Synthetic Applications",
    text:
      'Creates a draft mission: "Apply for the job". Applies the playbook procedure ' +
      '"Application": the plan gets its steps and roles. Nothing runs until you press Begin.',
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    ...over,
  };
}

function teamCard(): Record<string, unknown> {
  return {
    action_id: TEAM,
    kind: "operation",
    op: "team.assemble",
    risk: "reversible",
    label: "Assemble the team",
    verb: "Assemble",
    title: "Apply for the job",
    text:
      "Picks a specialist for every agent step that has none, the cheapest that qualifies " +
      "(plan revision 1). It writes a new plan revision and launches nothing.",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    mission: { id: MID },
  };
}

test("the confirmed draft card links its mission, and HQ continues by itself (#1950)", async ({
  page,
}) => {
  const fake = await shell(page, draftCard());
  await decideRoute(fake, page, {
    state: "done",
    summary:
      'Draft mission created (Apply for the job) with the procedure "Application" as plan ' +
      "revision 1. Nothing runs until you press Begin.",
    mission: { id: MID },
  });
  // The server starts the continuation before the card reads done: it is in the next read.
  await page.route(`**/api/ask/proposals/${PID}/decide`, async (r) => {
    const cont = {
      ...turnOf(
        "[BattleLab, not the Commander: the Commander confirmed your card.] " + MID,
        {
          answer: "The plan has 10 steps; confirm the card to staff them.",
          mission_matches: [{ id: MID, title: "Apply for the job", state: "draft", why: "" }],
        },
      ),
      via: "card",
      proposals: [teamCard()],
    };
    fake.conversations[0].turns.push(cont as never);
    await r.fallback();
  });
  const c = await open(page);
  await expect(c.getByTestId("ask-operation-mission")).toHaveCount(0); // no mission yet
  await c.getByTestId("ask-operation-confirm").click();
  const out = c.getByTestId("ask-operation-outcome");
  await expect(out).toContainText("Done", { timeout: 20_000 });
  const link = out.getByTestId("ask-operation-mission");
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", missionLink(MID));
  await minTarget(link);

  const panel = page.getByTestId("ask-turns");
  const cont = panel.getByTestId("ask-continuation");
  await expect(cont).toBeVisible({ timeout: 20_000 });
  await expect(cont).toContainText("After your confirm");
  // Not the Commander's words: one "You" bubble (the first question), and the note never shown.
  await expect(panel.getByRole("article", { name: "You" })).toHaveCount(1);
  await expect(panel).not.toContainText("not the Commander");

  const team = panel.getByTestId("ask-operation").nth(1);
  const teamLink = team.getByTestId("ask-operation-mission");
  await expect(teamLink).toHaveAttribute("href", missionLink(MID));
  await minTarget(teamLink);
  const row = panel.getByTestId("ask-mission-match");
  await expect(row).toContainText("Apply for the job");
  await expect(row.getByRole("link", { name: "Open mission Apply for the job" })).toHaveAttribute(
    "href",
    missionLink(MID),
  );
  await noSidewaysScroll(page);

  await link.click();
  await expect(page).toHaveURL(new RegExp(`${MISSION_PATH}(\\?|$)`));
});

test("a pending team card links the mission it changes (#1950)", async ({ page }) => {
  const fake = await shell(page, { ...teamCard(), action_id: PID });
  await decideRoute(fake, page, { state: "done", summary: "Saved as plan revision 2." });
  const c = await open(page);
  const link = c.getByTestId("ask-operation-mission");
  await expect(link).toHaveAttribute("href", missionLink(MID));
  await minTarget(link);
  await c.getByTestId("ask-operation-confirm").click();
  const out = c.getByTestId("ask-operation-outcome");
  await expect(out).toContainText("Saved as plan revision 2.", { timeout: 20_000 });
  await expect(out.getByTestId("ask-operation-mission")).toHaveAttribute(
    "href",
    missionLink(MID),
  );
  await noSidewaysScroll(page);
});

test("a malformed mission id on a card is no link at all (#1950)", async ({ page }) => {
  await shell(
    page,
    draftCard({ state: "done", summary: "Draft mission created.", mission: { id: "../x" } }),
  );
  const c = await open(page);
  await expect(c.getByTestId("ask-operation-outcome")).toContainText("Draft mission created.");
  await expect(c.getByTestId("ask-operation-mission")).toHaveCount(0);
});
