/** An API session shared with View between two REAL instances, through the pages (#1246 P3, #2055).
 *
 *  Nothing is mocked. Two app instances are started by this test and paired through Settings →
 *  Peers. alpha hosts a real API session: the in-tree `codex-api` client, each generation the
 *  real worker process, on a scripted Codex (`tests/fake_native.py`, see `federation_instance.py`)
 *  that answers `echo:<text>`. alpha's operator talks to it in the app and shares it with beta
 *  through the header's Share → Peer… and the dialog. beta's operator finds it on the session
 *  list's Shared tab, opens it and reads the turn alpha's agent produced: the owner's real
 *  projection, sent over the real peer channel, read through beta's real receiving routes. A new
 *  turn on alpha arrives by itself; a revoke in the dialog ends it on both.
 *
 *  The mocked spec (share-peer.spec.ts) covers every state; this one proves the pages and the two
 *  servers agree. */
import { expect, test } from "@playwright/test";
import { PY_STACK_AVAILABLE } from "./appmode";
import {
  ACROSS_MS,
  dumpLogs,
  openNewApiSession,
  openShareDialog,
  openSharedPane,
  say,
  shareWithBeta,
  twoOperators,
} from "./federationJourney";
import { startFederationPair, type FederationPair } from "./federationPair";
import { HOOK_TIMEOUT_MS } from "./harness";
import { SESSIONS_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";

test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

let pair: FederationPair;

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  pair = await startFederationPair({ api: true });
});
test.afterAll(async () => {
  await pair?.stop();
});

test("a real API session is shared View, read on the other instance, and revoked", async ({ browser }) => {
  const ops = await twoOperators(browser, pair);
  const { alpha, beta, alphaId, betaId } = ops;
  try {
    // alpha's operator and alpha's agent: a real turn, answered by the real worker.
    const local = await openNewApiSession(alpha, pair.a);
    await say(local, "What does the importer do?");
    const turns = local.getByTestId("structured-turn");
    await expect(turns).toHaveCount(1);
    await expect(turns.first()).toContainText("echo:What does the importer do?", { timeout: 30_000 });

    // Nothing is shared until the operator shares it.
    await beta.goto(`${pair.b.origin}${SESSIONS_PATH}`);
    await expect(beta.locator("aside.sidebar")).toBeVisible();
    await expect(beta.getByTestId("remote-row")).toHaveCount(0);

    await shareWithBeta(alpha, "View");

    // beta's operator: the row on the Shared tab, then the pane.
    const remote = await openSharedPane(beta, pair.b, alphaId);
    await expect(remote.getByTestId("peer-badge")).toHaveText("alpha");
    await expect(remote.getByTestId("remote-banner")).toContainText("Shared by alpha · view only · until");
    const theirs = remote.locator("[data-turn]");
    await expect(theirs).toHaveCount(1);
    await expect(theirs.first()).toContainText("What does the importer do?");
    await expect(theirs.first()).toContainText("echo:What does the importer do?");
    // View: nothing to type into, and the foot says so.
    await expect(remote.getByTestId("remote-view-only")).toHaveText("View only. Ask alpha's operator for more access.");
    await expect(remote.getByTestId("remote-composer")).toHaveCount(0);
    await expect(remote.locator("textarea, input, [contenteditable='true']")).toHaveCount(0);
    // It is not a local session of beta's: beta's own list has none.
    await beta.getByTestId("scope-tab-active").click();
    await expect(beta.locator('aside.sidebar a[href^="/s/"]')).toHaveCount(0);

    // A new turn on the owner arrives in the open pane by itself.
    await say(local, "And the exporter?");
    await expect(turns).toHaveCount(2);
    await expect(theirs).toHaveCount(2, { timeout: ACROSS_MS });
    await expect(theirs.nth(1)).toContainText("echo:And the exporter?", { timeout: ACROSS_MS });

    // Each instance's Settings → Peers counts it, from the real per-peer lists.
    const peers = await ops.ctxB.newPage();
    await peers.goto(`${pair.b.origin}${settingsPath("peers")}`);
    await expect(
      peers.locator(`[data-testid="peer-row"][data-peer="${alphaId}"]`).getByTestId("peer-shares-counts"),
    ).toHaveText("Sessions · 0 shared · 1 received", { timeout: ACROSS_MS });
    await peers.close();

    // Revoke, in the same dialog: beta's open pane says the share is gone and shows nothing.
    const dialog = await openShareDialog(alpha);
    await dialog.getByRole("button", { name: "Revoke beta" }).click();
    await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(0);
    await expect(remote.getByTestId("remote-empty")).toHaveText("This share is no longer available.", {
      timeout: ACROSS_MS,
    });
    await expect(remote.getByTestId("remote-state")).toHaveText("unavailable");
    await expect(theirs).toHaveCount(0);
    await expect(beta.getByText("echo:What does the importer do?")).toHaveCount(0);
    // The row has left the Shared tab, also for a page that loads now.
    await expect(beta.getByTestId("remote-row")).toHaveCount(0, { timeout: ACROSS_MS });
    await beta.goto(`${pair.b.origin}${SESSIONS_PATH}`);
    await expect(beta.locator("aside.sidebar")).toBeVisible();
    await expect(beta.getByTestId("remote-row")).toHaveCount(0);
    // The owner's session is untouched by any of it.
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(turns).toHaveCount(2);
    void betaId;
  } catch (e) {
    dumpLogs(pair);
    throw e;
  } finally {
    await ops.close();
  }
});
