/** Suggest and Send on an API session shared between two REAL instances, through the pages
 *  (#1246 P5, #2061).
 *
 *  Nothing is mocked. Two app instances are started by this test and paired through Settings →
 *  Peers; alpha hosts a real API session on a scripted Codex that answers `echo:<text>`
 *  (`federation_instance.py`). The operator of alpha shares it with Suggest: beta's operator
 *  suggests a message, alpha's operator sees the card and approves it, and only then does the
 *  agent get it; the turn carries beta's name on both instances. Then alpha's operator raises the
 *  grant to Send (the risk box ticked, as only they can): beta's message reaches the agent
 *  directly, attributed the same way. Lowering the grant takes beta's composer away.
 *
 *  The mocked spec (peer-input.spec.ts) covers every state; this one proves the pages and the
 *  two servers agree. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { PY_STACK_AVAILABLE } from "./appmode";
import {
  ACROSS_MS,
  dumpLogs,
  openNewApiSession,
  openSharedPane,
  say,
  shareWithBeta,
  twoOperators,
} from "./federationJourney";
import { startFederationPair, type FederationPair } from "./federationPair";
import { HOOK_TIMEOUT_MS } from "./harness";

test.describe.configure({ mode: "serial" });
test.setTimeout(300_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

let pair: FederationPair;

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  pair = await startFederationPair({ api: true });
});
test.afterAll(async () => {
  await pair?.stop();
});

const SUGGESTED = "Please run the importer tests.";
const SENT = "Now run the exporter tests.";

/** Every message alpha's agent was actually given: the scripted Codex logs each frame it reads
 *  (`native-frames.jsonl` in the session's folder). */
function givenToTheAgent(): string[] {
  return readFileSync(join(pair.a.workdir, "native-frames.jsonl"), "utf8")
    .split("\n")
    .filter((line) => line.includes('"turn/start"'));
}

test("a peer suggests and the owner approves; then a peer sends; a lowered grant ends both", async ({ browser }) => {
  const ops = await twoOperators(browser, pair);
  const { alpha, beta, alphaId } = ops;
  try {
    const local = await openNewApiSession(alpha, pair.a);
    await say(local, "What does the importer do?");
    const turns = local.getByTestId("structured-turn");
    await expect(turns.first()).toContainText("echo:What does the importer do?", { timeout: 30_000 });

    // ── Suggest ──
    await shareWithBeta(alpha, "Suggest");
    const remote = await openSharedPane(beta, pair.b, alphaId);
    await expect(remote.getByTestId("remote-banner")).toContainText("Shared by alpha · can suggest · until");
    const theirs = remote.locator("[data-turn]");
    await expect(theirs).toHaveCount(1);
    const composer = remote.getByTestId("remote-composer");
    await expect(composer).toHaveAttribute("data-kind", "suggest");
    await remote.getByTestId("remote-composer-text").fill(SUGGESTED);
    await remote.getByTestId("remote-composer-go").click();
    const mine = remote.getByTestId("remote-suggestion");
    await expect(mine).toHaveCount(1);
    await expect(mine).toContainText(SUGGESTED);
    await expect(mine).toContainText("waiting for alpha's operator");
    await expect(remote.getByTestId("remote-composer-text")).toHaveValue("");

    // alpha's operator sees the card. Nothing has reached the agent, on either page.
    const card = alpha.getByTestId("peer-suggestion");
    await expect(card).toHaveCount(1, { timeout: ACROSS_MS });
    await expect(card.getByTestId("peer-suggestion-text")).toHaveText(SUGGESTED);
    await expect(card).toContainText("beta");
    await expect(turns).toHaveCount(1);
    await expect(theirs).toHaveCount(1);
    expect(givenToTheAgent().join("\n")).not.toContain(SUGGESTED);

    // Approve: the turn runs, labelled with the peer, on the owner…
    await card.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(card).toHaveCount(0);
    await expect(turns).toHaveCount(2, { timeout: 30_000 });
    await expect(turns.nth(1)).toContainText(SUGGESTED);
    await expect(turns.nth(1)).toContainText(`echo:${SUGGESTED}`, { timeout: 30_000 });
    await expect(turns.nth(1).getByTestId("peer-attribution")).toHaveText(/^beta\s*·\s*peer$/i);
    await expect(turns.nth(0).getByTestId("peer-attribution")).toHaveCount(0); // the operator's own
    // …and on the instance that suggested it, under the name the owner has for it.
    await expect(theirs).toHaveCount(2, { timeout: ACROSS_MS });
    await expect(theirs.nth(1)).toContainText(`echo:${SUGGESTED}`, { timeout: ACROSS_MS });
    await expect(theirs.nth(1).getByTestId("peer-attribution")).toHaveText(/^beta\s*·\s*peer$/i);
    await expect(theirs.nth(0).getByTestId("peer-attribution")).toHaveCount(0);
    await expect(remote.locator('[data-testid="remote-suggestion"][data-state="approved"]')).toHaveCount(1, {
      timeout: ACROSS_MS,
    });
    // A suggest grant does not send: there is no Send composer to press.
    await expect(composer).toHaveAttribute("data-kind", "suggest");

    // ── Send: the operator raises the same grant, with the risk box ticked ──
    await shareWithBeta(alpha, "Send");
    await expect(remote.getByTestId("remote-banner")).toContainText("Shared by alpha · can send", {
      timeout: ACROSS_MS,
    });
    await expect(composer).toHaveAttribute("data-kind", "send");
    await expect(remote.getByTestId("remote-countdown")).toHaveText(/^ends in (5\d min|1 h)$/);
    await remote.getByTestId("remote-composer-text").fill(SENT);
    await remote.getByTestId("remote-composer-go").click();
    // No card and no approval: it is a turn on the owner, attributed to the peer.
    await expect(turns).toHaveCount(3, { timeout: ACROSS_MS });
    await expect(turns.nth(2)).toContainText(`echo:${SENT}`, { timeout: 30_000 });
    await expect(turns.nth(2).getByTestId("peer-attribution")).toHaveText(/^beta\s*·\s*peer$/i);
    await expect(alpha.getByTestId("peer-suggestion")).toHaveCount(0);
    // The sender reads it once, as the turn, with the reply.
    await expect(theirs).toHaveCount(3, { timeout: ACROSS_MS });
    await expect(theirs.nth(2)).toContainText(`echo:${SENT}`, { timeout: ACROSS_MS });
    await expect(theirs.nth(2).getByTestId("peer-attribution")).toHaveText(/^beta\s*·\s*peer$/i);
    await expect(remote.getByTestId("remote-own-message")).toHaveCount(0);
    await expect(remote.getByTestId("remote-composer-text")).toHaveValue("");
    // The agent was given exactly three messages: one per turn, none twice.
    expect(givenToTheAgent()).toHaveLength(3);

    // ── Lowered to View: nothing can be sent any more ──
    // With a draft in the box the composer stays, DISABLED, so the text is not lost (#2061)…
    await remote.getByTestId("remote-composer-text").fill("One more thing");
    await shareWithBeta(alpha, "View");
    await expect(remote.getByTestId("remote-banner")).toContainText("Shared by alpha · view only", {
      timeout: ACROSS_MS,
    });
    await expect(remote.getByTestId("remote-composer-note")).toHaveText(
      "Send access ended. Your text is kept here; ask for access again.",
    );
    await expect(remote.getByTestId("remote-composer-text")).toBeDisabled();
    await expect(remote.getByTestId("remote-composer-text")).toHaveValue("One more thing");
    await expect(remote.getByTestId("remote-composer-go")).toBeDisabled();
    // …and a page that opens the share now has no composer at all.
    await beta.reload();
    await expect(remote.getByTestId("remote-state")).toHaveText("connected", { timeout: ACROSS_MS });
    await expect(theirs).toHaveCount(3);
    await expect(remote.getByTestId("remote-view-only")).toHaveText("View only. Ask alpha's operator for more access.");
    await expect(remote.getByTestId("remote-composer")).toHaveCount(0);
    await expect(remote.locator("textarea")).toHaveCount(0);
    await expect(turns).toHaveCount(3);
    expect(givenToTheAgent()).toHaveLength(3);
  } catch (e) {
    dumpLogs(pair);
    throw e;
  } finally {
    await ops.close();
  }
});
