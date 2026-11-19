/** A terminal's screen shared between two REAL instances, through the pages (#1246 P4b, #2060).
 *
 *  Nothing is mocked. Two app instances are started by this test and paired through Settings →
 *  Peers. alpha hosts a real terminal session: a real `dtach` master (running `cat` where the
 *  shell provider runs `bash`, so the bytes are the same on every host), produced by the harness
 *  the way `tests/test_federation_terminal.py`'s two-instance test produces one. alpha's operator
 *  opens it in the app (the real `/ws/term` attach and a real xterm), types into it, and shares
 *  it with beta through the pane's menu and the dialog. beta's operator opens the row and reads
 *  the screen: frames rendered by alpha's real exporter, sent over the real peer channel, read
 *  through beta's real `…/frame` route. New output on alpha arrives as a new frame; a revoke in
 *  the dialog ends it.
 *
 *  The mocked spec (share-terminal.spec.ts) covers every state; this one proves the pages and
 *  the two servers agree. */
import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { PY_STACK_AVAILABLE } from "./appmode";
import { startFederationPair, type FederationPair } from "./federationPair";
import { clickHeadAction } from "./headActions";
import { HOOK_TIMEOUT_MS } from "./harness";
import { openLinksFullScreen } from "./linkMode";
import { SESSIONS_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";

const DTACH_AVAILABLE = (() => {
  try {
    execFileSync("dtach", ["--version"], { stdio: "pipe" });
    return true;
  } catch (e) {
    // `dtach --version` prints its version and exits non-zero on some builds: it ran, so it is
    // there. Only "no such file" means it is not.
    return (e as NodeJS.ErrnoException).code !== "ENOENT";
  }
})();

const TERMINAL = "0e2e7e21-0000-4000-8000-00000000f4b0";
const SHARE_TERMINAL = "Share this terminal's screen with a paired instance";

test.describe.configure({ mode: "serial" });
test.setTimeout(180_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");
test.skip(!DTACH_AVAILABLE, "needs dtach: a real terminal session is a dtach master");

let pair: FederationPair;

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  pair = await startFederationPair({ terminal: TERMINAL });
});
test.afterAll(async () => {
  await pair?.stop();
});

/** Pair the two through Settings → Peers, as `appmode-federation-pair.spec.ts` proves in detail. */
async function pairThroughThePages(alpha: Page, beta: Page): Promise<{ alphaId: string; betaId: string }> {
  const selfId = async (page: Page, origin: string) => {
    await page.goto(`${origin}${settingsPath("peers")}`);
    return (await page.getByTestId("peers-self").getByTestId("peer-id").innerText()).replace(/\s+/g, "");
  };
  const alphaId = await selfId(alpha, pair.a.origin);
  const betaId = await selfId(beta, pair.b.origin);
  await alpha.getByRole("button", { name: "Create invite" }).click();
  const invite = await alpha.getByTestId("invite-string").innerText();
  await beta.getByLabel("Invite").fill(invite);
  await beta.getByRole("button", { name: "Pair", exact: true }).click();
  await alpha.getByRole("button", { name: "They match" }).click();
  await beta.getByRole("button", { name: "They match" }).click();
  const row = (page: Page, id: string) => page.locator(`[data-testid="peer-row"][data-peer="${id}"]`);
  await expect(row(alpha, betaId)).toHaveAttribute("data-state", "connected", { timeout: 45_000 });
  await expect(row(beta, alphaId)).toHaveAttribute("data-state", "connected", { timeout: 45_000 });
  return { alphaId, betaId };
}

test("a real terminal is shared through the dialog, watched on the other instance, and revoked", async ({
  browser,
}) => {
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  try {
    const alpha = await ctxA.newPage();
    const beta = await ctxB.newPage();
    // The "Full screen or map?" choice (#1232) is made, as the suite's storage state makes it
    // for the preview origin: this spec opens a session URL on the instance's own origin.
    await openLinksFullScreen(ctxA);
    const { alphaId } = await pairThroughThePages(alpha, beta);

    // alpha's operator opens the real terminal session: the app's own attach, a real xterm.
    await alpha.goto(`${pair.a.origin}/s/shell/${TERMINAL}`);
    const term = alpha.locator(".xterm-rows");
    await expect(term).toBeVisible({ timeout: 30_000 });
    // Attached as the owner of this view: Repaint is only enabled on a connected terminal.
    await expect(alpha.getByRole("button", { name: "Repaint screen" })).toBeEnabled({ timeout: 30_000 });
    const typeLine = async (text: string) => {
      await alpha.locator(".xterm-helper-textarea").focus();
      await alpha.keyboard.type(text);
      await alpha.keyboard.press("Enter");
    };
    // `cat` answers each line with the same line, so the screen shows it twice: the terminal's
    // echo of what was typed, then cat's own output.
    await typeLine("hello from the owner");
    await expect(term).toContainText("hello from the owner");

    // Share it with beta through the pane's menu and the dialog.
    await clickHeadAction(alpha, SHARE_TERMINAL);
    const dialog = alpha.getByRole("dialog", { name: "Share with a peer" });
    await expect(dialog.getByTestId("share-peer-limit")).toBeVisible();
    await expect(dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio")).toHaveCount(1);
    const tile = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio", { name: /beta/ });
    await expect(tile).toContainText("connected");
    await tile.click();
    await dialog.getByTestId("share-peer-go").click();
    await expect(dialog.getByTestId("share-peer-done")).toContainText("Shared with beta until");
    await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1);
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);

    // beta's operator: the row arrives (the owner's hint refreshes the mirror; the page reads the
    // list when it loads), tagged SCREEN, and opens the screen pane.
    const row = beta.getByTestId("remote-row").filter({ has: beta.getByTestId("screen-tag") });
    await expect(async () => {
      await beta.goto(`${pair.b.origin}${SESSIONS_PATH}`);
      // Shared sessions are on the list's Shared tab (#2198), offered once one is shared.
      await beta.getByTestId("scope-tab-shared").click({ timeout: 5_000 });
      await expect(row).toHaveCount(1, { timeout: 5_000 });
    }).toPass({ timeout: 60_000 });
    await expect(row.getByTestId("peer-badge")).toHaveText("alpha");
    await row.locator("a").click();
    await expect(beta).toHaveURL(new RegExp(`/remote/${alphaId}/[A-Za-z0-9_-]+$`));
    const pane = beta.getByTestId("remote-pane");
    await expect(pane.locator("header").getByTestId("screen-tag")).toBeVisible();
    await expect(pane.getByTestId("remote-banner")).toContainText("Shared by alpha · screen only · until");
    await expect(pane.getByTestId("remote-state")).toHaveText("connected");
    await expect(pane.getByTestId("remote-screen-only")).toHaveText("Screen only. No typing, no scrollback.");
    const screen = pane.getByTestId("remote-screen");
    /** The rows of the frame that carry anything, top to bottom. */
    const written = async () =>
      (await pane.getByTestId("remote-screen-text").locator("> div").allTextContents()).filter(
        (line) => line.trim() !== "",
      );

    // The owner's exporter is strict (#2056): what was written before the terminal's geometry
    // last settled (the attach above resizes and nudges it) is not vouched for, and the pane
    // then says, truthfully, that the screen can't be shown. It can be shown again once the
    // program has redrawn its screen. `cat` is the program here, so the operator has it write an
    // erase-and-home (cat prints the line it is given, raw), and then the line to look for.
    await expect(async () => {
      await alpha.locator(".xterm-helper-textarea").focus();
      await alpha.keyboard.press("Escape");
      await alpha.keyboard.type("[2J");
      await alpha.keyboard.press("Escape");
      await typeLine("[H");
      await typeLine("hello from the owner");
      await expect.poll(written, { timeout: 10_000 }).toEqual(["hello from the owner", "hello from the owner"]);
    }).toPass({ timeout: 90_000 });
    await expect(screen).toBeVisible();
    // The owner's real screen, as text, at the geometry the owner's page gave the terminal.
    const dims = (await pane.getByTestId("remote-screen-dims").innerText()).match(/^(\d+) × (\d+)$/)!;
    expect(Number(dims[1])).toBeGreaterThanOrEqual(20);
    await expect(pane.getByTestId("remote-screen-text").locator("> div")).toHaveCount(Number(dims[2]));

    // Typing at beta's pane reaches nobody.
    await screen.click();
    await beta.keyboard.type("typed on beta");
    await beta.keyboard.press("Enter");

    // New output on the owner is seen as a new frame.
    await typeLine("a second line");
    await expect(term).toContainText("a second line");
    await expect.poll(written, { timeout: 30_000 }).toEqual([
      "hello from the owner",
      "hello from the owner",
      "a second line",
      "a second line",
    ]);
    await expect(term).not.toContainText("typed on beta");
    await expect(pane.getByTestId("remote-screen-marker")).toHaveCount(0, { timeout: 30_000 });

    // Revoke, in the same dialog: beta's pane says the share is gone and shows nothing.
    await clickHeadAction(alpha, SHARE_TERMINAL);
    await alpha
      .getByRole("dialog", { name: "Share with a peer" })
      .getByRole("button", { name: "Revoke beta" })
      .click();
    await expect(alpha.getByTestId("share-peer-grant")).toHaveCount(0);
    await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.", {
      timeout: 30_000,
    });
    await expect(pane.getByTestId("remote-state")).toHaveText("unavailable");
    await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
    await expect(beta.getByText("hello from the owner")).toHaveCount(0);
    await expect(beta.getByTestId("remote-row")).toHaveCount(0);
  } catch (e) {
    console.error(`alpha:\n${pair.logs.alpha}\nbeta:\n${pair.logs.beta}`);
    throw e;
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});
