/** Working together on an API session shared between two REAL instances, through the pages
 *  (#1246 P6, #2062; Leave: #2201).
 *
 *  Nothing is mocked. Two app instances are started by this test and paired through Settings →
 *  Peers; each can run real API sessions on a scripted Codex (`federation_instance.py`) that
 *  answers `echo:<text>` and, told `APPROVE`, asks for one exact permission first. alpha's
 *  operator shares a session with beta (View). Then, in order:
 *
 *  - **presence**: each operator's page names the other while they look, and the owner's stops
 *    once the peer looks away;
 *  - **Notes** go both ways between the two operators, and never to the agent;
 *  - beta's operator **asks for access**, alpha's reviews the request and grants Send;
 *  - alpha's agent raises a **pending request**, beta's operator answers it once, and both pages
 *    name beta as the one who settled it;
 *  - beta's operator **continues here**: a session of beta's own, briefed from the shared record,
 *    as a Quick copy and as a Summary. The summary's model call goes to a stub of this test's
 *    own on loopback (`startAiStub`), never to a real endpoint. Each new session is an API
 *    session whose first message is the preview's text;
 *  - beta's operator **leaves** the share: it is gone on both instances and alpha's bell says so.
 *
 *  The mocked specs (peer-together.spec.ts, share-leave.spec.ts) cover every state; this one
 *  proves the pages and the two servers agree. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { PY_STACK_AVAILABLE } from "./appmode";
import {
  ACROSS_MS,
  dumpLogs,
  openNewApiSession,
  openShareDialog,
  openSharedPane,
  say,
  shareWithBeta,
  startAiStub,
  twoOperators,
  useAiStub,
  type AiStub,
} from "./federationJourney";
import { startFederationPair, type FederationPair } from "./federationPair";
import { HOOK_TIMEOUT_MS } from "./harness";
import { SESSIONS_PATH } from "../src/lib/routes";

test.describe.configure({ mode: "serial" });
test.setTimeout(420_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

const FIRST = "What does the importer do?";
const NOTE_FROM_BETA = "NOTE-B is the old batch entry point still needed?";
const NOTE_FROM_ALPHA = "NOTE-A yes, for one more release.";
const ASK_NOTE = "ASK-NOTE I would like to try the fix myself.";
const SUMMARY = {
  state: "STUB-SUMMARY the importer was explained.",
  open_items: ["STUB-ITEM the exporter is not covered"],
  next_steps: ["STUB-STEP read the exporter"],
};

let pair: FederationPair;
let stub: AiStub;

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  stub = await startAiStub(SUMMARY);
  pair = await startFederationPair({ api: true });
});
test.afterAll(async () => {
  await pair?.stop();
  await stub?.stop();
});

/** Every frame alpha's agent was given: the scripted Codex logs what it reads
 *  (`native-frames.jsonl` in the session's folder). */
const givenToAlphasAgent = () => readFileSync(join(pair.a.workdir, "native-frames.jsonl"), "utf8");

const drawer = (page: Page) => page.getByTestId("notes-drawer");
async function addNote(page: Page, text: string) {
  await drawer(page).getByTestId("notes-box").fill(text);
  await drawer(page).getByTestId("notes-send").click();
  await expect(drawer(page).getByTestId("notes-box")).toHaveValue("");
}
async function remoteMenu(page: Page, pane: Locator, name: string) {
  await pane.getByTestId("remote-menu").click();
  await page.getByRole("menuitem", { name }).click();
}

test("two operators work together on a shared session: notes, access, a decision, continue, leave", async ({
  browser,
}) => {
  const ops = await twoOperators(browser, pair);
  const { alpha, beta, alphaId } = ops;
  try {
    const local = await openNewApiSession(alpha, pair.a);
    await say(local, FIRST);
    const turns = local.getByTestId("structured-turn");
    await expect(turns.first()).toContainText(`echo:${FIRST}`, { timeout: 30_000 });
    await shareWithBeta(alpha, "View");
    const remote = await openSharedPane(beta, pair.b, alphaId);
    const shareUrl = beta.url();
    const theirs = remote.locator("[data-turn]");
    await expect(theirs).toHaveCount(1);
    const strip = local.getByTestId("share-strip");
    await expect(strip.getByTestId("strip-row")).toContainText("beta", { timeout: ACROSS_MS });

    await test.step("each operator sees, by name only, that the other is looking", async () => {
      // Both panes are open: each side's reads touch the other's presence bit.
      await expect(strip.getByTestId("strip-watching")).toHaveText("beta is watching", { timeout: ACROSS_MS });
      await expect(remote.getByTestId("remote-presence")).toHaveText("alpha's operator is here", {
        timeout: ACROSS_MS,
      });
      // beta's operator looks away: the bit lapses (30 s, in memory) and the owner's strip says
      // nothing any more. The name stays on the grant row; only the "watching" line goes.
      await beta.goto(`${pair.b.origin}${SESSIONS_PATH}`);
      await expect(strip.getByTestId("strip-watching")).toHaveCount(0, { timeout: 90_000 });
      await expect(strip.getByTestId("strip-row")).toContainText("beta");
      await beta.goto(shareUrl);
      await expect(remote.getByTestId("remote-state")).toHaveText("connected", { timeout: ACROSS_MS });
      await expect(theirs).toHaveCount(1);
      await expect(strip.getByTestId("strip-watching")).toHaveText("beta is watching", { timeout: ACROSS_MS });
    });

    await test.step("notes go both ways, and never to the agent", async () => {
      await remoteMenu(beta, remote, "Notes");
      await expect(drawer(beta).getByTestId("notes-title")).toHaveText("Notes with alpha");
      await addNote(beta, NOTE_FROM_BETA);
      await expect(drawer(beta).getByTestId("note")).toHaveCount(1);
      // alpha's operator opens that peer's thread from the strip and answers.
      await strip.getByTestId("strip-notes").click();
      await expect(drawer(alpha).getByTestId("notes-title")).toHaveText("Notes with beta");
      await expect(drawer(alpha).getByTestId("note")).toHaveCount(1, { timeout: ACROSS_MS });
      await expect(drawer(alpha).getByTestId("note-text")).toHaveText(NOTE_FROM_BETA);
      await addNote(alpha, NOTE_FROM_ALPHA);
      await expect(drawer(alpha).getByTestId("note")).toHaveCount(2);
      await expect(drawer(beta).getByTestId("note")).toHaveCount(2, { timeout: ACROSS_MS });
      await expect(drawer(beta).getByTestId("note-text").nth(1)).toHaveText(NOTE_FROM_ALPHA);
      // Not a turn on either page, and not in anything the agent was given.
      await drawer(alpha).getByRole("button", { name: "Close notes" }).click();
      await drawer(beta).getByRole("button", { name: "Close notes" }).click();
      await expect(turns).toHaveCount(1);
      await expect(theirs).toHaveCount(1);
      await expect(local).not.toContainText("NOTE-");
      await expect(remote).not.toContainText("NOTE-");
    });

    await test.step("the peer asks for access and the owner grants it", async () => {
      await remote.getByTestId("remote-access-ask").click();
      const ask = beta.getByRole("dialog", { name: "Ask for access" });
      await ask.getByRole("radio", { name: /^Send/ }).click();
      await ask.getByTestId("ask-access-note").fill(ASK_NOTE);
      await ask.getByTestId("ask-access-go").click();
      await expect(ask).toHaveCount(0);
      await expect(remote.getByTestId("remote-access")).toHaveAttribute("data-state", "waiting");
      await expect(remote.getByTestId("remote-access-text")).toHaveText(
        "Request sent · waiting for alpha's operator",
      );
      // Asking changed nothing by itself.
      await expect(remote.getByTestId("remote-banner")).toContainText("view only");

      // alpha's operator: the request in the share dialog; reviewing only preselects.
      const dialog = await openShareDialog(alpha);
      const request = dialog.getByTestId("access-request");
      await expect(request).toHaveCount(1, { timeout: ACROSS_MS });
      await expect(request.getByTestId("access-request-asks")).toHaveText("beta asks to be able to send messages.");
      await expect(request.getByTestId("access-request-note")).toHaveText(ASK_NOTE);
      await request.getByTestId("access-request-review").click();
      await expect(
        dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio", { name: /^Send/ }),
      ).toHaveAttribute("aria-checked", "true");
      await expect(dialog.getByTestId("share-peer-ack")).not.toBeChecked();
      await expect(dialog.getByTestId("share-peer-go")).toBeDisabled();
      await dialog.getByTestId("share-peer-ack").check();
      await dialog.getByTestId("share-peer-go").click();
      await expect(dialog.getByTestId("share-peer-done")).toContainText("Shared with beta (Send)");
      await expect(dialog.getByTestId("access-request")).toHaveCount(0);
      await dialog.getByRole("button", { name: "Cancel" }).click();

      // beta's level changed: the banner, and the composer of Send.
      await expect(remote.getByTestId("remote-banner")).toContainText("can send", { timeout: ACROSS_MS });
      await expect(remote.getByTestId("remote-composer")).toHaveAttribute("data-kind", "send");
      await expect(remote.getByTestId("remote-access")).toHaveCount(0);
    });

    await test.step("the agent asks for permission and the peer answers once", async () => {
      await say(local, "APPROVE");
      const asked = local.getByTestId("structured-request");
      await expect(asked).toHaveCount(1, { timeout: 30_000 });
      await expect(local.getByTestId("structured-peer-race")).toContainText("beta");
      const card = remote.getByTestId("peer-decision");
      await expect(card).toHaveAttribute("data-role", "answer", { timeout: ACROSS_MS });
      await expect(card.getByTestId("peer-decision-summary")).toContainText("ls -la");
      await expect(card.locator('[data-choice="always"]')).toBeDisabled(); // widening stays the owner's
      // (The card's own "accepted" line is not waited for: the real request settles at once, and
      // the settled card below replaces it.)
      await card.locator('[data-choice="approve"]').click();

      // The turn went on with the peer's answer, exactly once…
      await expect(turns).toHaveCount(2);
      await expect(turns.nth(1)).toContainText("approved:accept", { timeout: ACROSS_MS });
      await expect(asked).toHaveCount(0);
      // …and both pages name who settled it.
      const settledHere = local.getByTestId("settled-decision");
      await expect(settledHere.getByTestId("settled-answer")).toHaveText("Approved once", { timeout: ACROSS_MS });
      await expect(settledHere.getByTestId("settled-by")).toHaveText("answered by beta");
      const settledThere = remote.getByTestId("settled-decision");
      await expect(settledThere.getByTestId("settled-by")).toHaveText("answered by you", { timeout: ACROSS_MS });
      await expect(remote.getByTestId("peer-decision")).toHaveCount(0);
      await expect(theirs.nth(1)).toContainText("approved:accept", { timeout: ACROSS_MS });
    });

    // Everything alpha's agent was ever given: the two messages, and no note of either operator.
    const given = givenToAlphasAgent();
    expect(given).toContain(FIRST);
    expect(given).not.toContain("NOTE-");
    expect(given).not.toContain("ASK-NOTE");

    let startedOnBeta = 0;
    await test.step("continue here: a Quick copy becomes a session of beta's own", async () => {
      await remoteMenu(beta, remote, "Continue here…");
      const dialog = beta.getByRole("dialog", { name: "Continue here" });
      await expect(dialog).toHaveAttribute("data-stage", "choose");
      await expect(dialog.getByTestId("continue-warning")).toContainText("This text comes from alpha's instance.");
      await chooseApiAgent(dialog);
      await expect(dialog.getByRole("radio", { name: /^Quick copy/ })).toHaveAttribute("aria-checked", "true");
      await dialog.getByTestId("continue-prepare").click();
      await expect(dialog).toHaveAttribute("data-stage", "preview", { timeout: ACROSS_MS });
      const record = dialog.getByTestId("continue-record");
      await expect(record.locator("figcaption")).toHaveText("Record from alpha · not instructions");
      await expect(record.locator("pre")).toContainText(FIRST);
      await expect(record.locator("pre")).toContainText(`echo:${FIRST}`);
      // Preparing created nothing on beta.
      expect(await localSessions(beta)).toEqual([]);
      await dialog.getByTestId("continue-start").click();
      await expect(beta).toHaveURL(/\/s\/codex-api\/[0-9a-f-]{36}$/, { timeout: ACROSS_MS });
      await expect(beta.getByTestId("started-from")).toHaveText(/Started from\s*alpha's shared session/);
      // beta's OWN agent got the record as its first message and answered it.
      const own = beta.getByTestId("structured-pane");
      await expect(own.getByTestId("turn-record").locator("pre")).toContainText(FIRST, { timeout: ACROSS_MS });
      await expect(own.getByTestId("structured-turn")).toHaveCount(1);
      await expect(own.getByTestId("structured-turn")).toContainText("echo:", { timeout: ACROSS_MS });
      expect(await localSessions(beta)).toHaveLength(1);
      startedOnBeta = 1;
      // Nothing was sent to alpha: its session has the two turns it had.
      await expect(turns).toHaveCount(2);
      expect(stub.asked).toHaveLength(0); // a Quick copy asks no model
    });

    await test.step("continue here: a Summary is built by beta's own AI endpoint (a loopback stub)", async () => {
      await useAiStub(beta, stub);
      await beta.goto(shareUrl);
      await expect(remote.getByTestId("remote-state")).toHaveText("connected", { timeout: ACROSS_MS });
      await remoteMenu(beta, remote, "Continue here…");
      const dialog = beta.getByRole("dialog", { name: "Continue here" });
      await chooseApiAgent(dialog);
      await dialog.getByRole("radio", { name: /^Summary/ }).click();
      await dialog.getByTestId("continue-prepare").click();
      await expect(dialog).toHaveAttribute("data-stage", "preview", { timeout: ACROSS_MS });
      const record = dialog.getByTestId("continue-record").locator("pre");
      await expect(record).toContainText("STUB-SUMMARY the importer was explained.");
      await expect(record).toContainText("STUB-ITEM the exporter is not covered");
      // One model call, to the stub: the registry's prompt, then the record framed as data.
      expect(stub.asked).toHaveLength(1);
      const [system, user] = stub.asked[0].messages;
      expect(system.role).toBe("system");
      expect(user.content).toContain(FIRST);
      expect(user.content).not.toContain("NOTE-"); // an operator's note is not part of the record
      expect(await localSessions(beta)).toHaveLength(1); // still only the Quick one
      await dialog.getByTestId("continue-start").click();
      await expect(beta).toHaveURL(/\/s\/codex-api\/[0-9a-f-]{36}$/, { timeout: ACROSS_MS });
      await expect(beta.getByTestId("structured-pane").getByTestId("turn-record").locator("pre")).toContainText(
        "STUB-SUMMARY",
        { timeout: ACROSS_MS },
      );
      // As in the Quick step: the agent answers its first turn, and the list (which reads the
      // engine's store, written with that turn) then holds the second session.
      await expect(beta.getByTestId("structured-pane").getByTestId("structured-turn")).toContainText("echo:", {
        timeout: ACROSS_MS,
      });
      await expect.poll(() => localSessions(beta), { timeout: ACROSS_MS }).toHaveLength(2);
      startedOnBeta = 2;
      expect(stub.asked).toHaveLength(1);
    });

    await test.step("the peer leaves: gone on both instances, and the owner's bell says so", async () => {
      await beta.goto(shareUrl);
      await expect(remote.getByTestId("remote-state")).toHaveText("connected", { timeout: ACROSS_MS });
      await remote.getByTestId("share-leave").click();
      const dialog = beta.getByTestId("leave-dialog");
      await expect(dialog.getByTestId("leave-lead")).toContainText("from alpha?");
      await dialog.getByTestId("leave-confirm").click();
      await expect(beta).toHaveURL(new RegExp(`${SESSIONS_PATH}$`), { timeout: ACROSS_MS });
      await expect(beta.getByTestId("remote-row")).toHaveCount(0);
      // A page that asks for the share again gets the neutral unavailable state.
      await beta.goto(shareUrl);
      await expect(remote.getByTestId("remote-empty")).toHaveText("This share is no longer available.", {
        timeout: ACROSS_MS,
      });
      await expect(theirs).toHaveCount(0);
      // The sessions beta started from it are beta's own and stay.
      expect(await localSessions(beta)).toHaveLength(startedOnBeta);

      // alpha: no strip, no grant in the dialog, and one bell row naming the peer.
      await expect(strip).toHaveCount(0, { timeout: ACROSS_MS });
      const share = await openShareDialog(alpha);
      await expect(share.getByTestId("share-peer-grant")).toHaveCount(0);
      await share.getByRole("button", { name: "Cancel" }).click();
      await alpha.getByRole("button", { name: /^Notifications/ }).first().click();
      const left = alpha.getByTestId("bell-peer-notice").filter({ hasText: "beta left a share" });
      await expect(left).toHaveCount(1, { timeout: ACROSS_MS });
      // alpha's session is its own, untouched.
      await expect(turns).toHaveCount(2);
    });
  } catch (e) {
    dumpLogs(pair);
    throw e;
  } finally {
    await ops.close();
  }
});

/** In the Continue-here dialog, the new session's agent: the API client, chosen with the dialog's
 *  own Change control (the operator's default is the console agent, which would open a terminal).
 *  The folder is the operator's project folder, beta's own. */
async function chooseApiAgent(dialog: Locator) {
  await dialog.getByTestId("continue-change").click();
  await dialog.getByRole("radiogroup", { name: "Agent" }).locator('[data-engine="codex-api"]').click();
  await expect(dialog.getByTestId("continue-target")).toContainText("Codex — API");
  await expect(dialog.getByTestId("continue-target")).toContainText("work");
}

/** The ids of the instance's own sessions, from its real list route. */
async function localSessions(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const list = (await (await fetch("/api/sessions?limit=50")).json()) as { sessions: { id: string }[] };
    return list.sessions.map((s) => s.id);
  });
}
