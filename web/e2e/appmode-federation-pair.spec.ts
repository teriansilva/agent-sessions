/** Settings → Peers against the REAL server (#1246 P2, #2052): two app instances started by this
 *  test, paired through the page in two browsers, ending with each listing the other as connected.
 *
 *  Nothing is mocked. The invite is minted by one app and dialled by the other over the real
 *  `/ws/fed` door, the code both pages show is the one the Noise handshake produced, and
 *  "connected" is the real peer channel the two dialers settled on. The mocked spec
 *  (settings-peers.spec.ts) covers every state; this one proves the page and the server agree. */
import { expect, test, type Page } from "@playwright/test";
import { PY_STACK_AVAILABLE } from "./appmode";
import { startFederationPair, type FederationInstance, type FederationPair } from "./federationPair";
import { HOOK_TIMEOUT_MS } from "./harness";
import { settingsPath } from "../src/routes/settingsTabs";

test.describe.configure({ mode: "serial" });
test.setTimeout(120_000);
test.skip(!PY_STACK_AVAILABLE, "needs the Python stack (uv)");

let pair: FederationPair;

test.beforeAll(async () => {
  test.setTimeout(HOOK_TIMEOUT_MS);
  pair = await startFederationPair();
});
test.afterAll(async () => {
  await pair?.stop();
});

async function openPeers(page: Page, instance: FederationInstance): Promise<string> {
  await page.goto(`${instance.origin}${settingsPath("peers")}`);
  const self = page.getByTestId("peers-self");
  await expect(self).toContainText(instance.name);
  await expect(page.getByTestId("peers-reachable")).toHaveText("directly");
  await expect(page.getByTestId("peers-list")).toContainText("No paired instances yet.");
  return (await self.getByTestId("peer-id").innerText()).replace(/\s+/g, "");
}

test("two real instances pair through the page and list each other as connected", async ({
  browser,
}) => {
  // Two operators: separate contexts, nothing shared but what they read to each other.
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  try {
    const alpha = await ctxA.newPage();
    const beta = await ctxB.newPage();
    const alphaId = await openPeers(alpha, pair.a);
    const betaId = await openPeers(beta, pair.b);
    expect(alphaId).toMatch(/^[a-z2-7]{32}$/);
    expect(betaId).not.toBe(alphaId);

    // alpha invites.
    await alpha.getByRole("button", { name: "Create invite" }).click();
    const invite = await alpha.getByTestId("invite-string").innerText();
    expect(invite).toMatch(/^blfed1:/);
    await expect(alpha.getByTestId("invite-qr")).toBeVisible();
    await expect(alpha.getByTestId("invite-countdown")).toHaveText(/Valid for 9:\d\d · works once/);

    // beta accepts: the real dial, the real handshake.
    await beta.getByLabel("Invite").fill(invite);
    await beta.getByRole("button", { name: "Pair", exact: true }).click();

    // Both pages show the code, and it is the same code. alpha's invite card became it by itself.
    const codeOf = async (page: Page) => {
      const code = page.getByTestId("pairing-code").locator("span:not(.nj-sr-only)");
      await expect(code).toHaveCount(2);
      return (await code.allInnerTexts()).join("");
    };
    const onBeta = await codeOf(beta);
    const onAlpha = await codeOf(alpha);
    expect(onAlpha).toMatch(/^\d{6}$/);
    expect(onAlpha).toBe(onBeta);
    await expect(alpha.getByTestId("peers-invite")).toHaveCount(0);
    // Each names the other: the suggested name and the peer id the other page shows as its own.
    await expect(alpha.getByTestId("pairing-name")).toHaveText("beta");
    await expect(beta.getByTestId("pairing-name")).toHaveText("alpha");
    await expect(alpha.getByTestId("peers-pairing").getByTestId("peer-id")).toHaveText(betaId);
    await expect(beta.getByTestId("peers-pairing").getByTestId("peer-id")).toHaveText(alphaId);

    // One operator alone stores nothing, on either side.
    await alpha.getByRole("button", { name: "They match" }).click();
    await expect(alpha.getByTestId("pairing-waiting")).toContainText(
      "Confirmed here · waiting for the other operator",
    );
    await expect(alpha.getByTestId("peer-row")).toHaveCount(0);
    await expect(beta.getByTestId("peer-row")).toHaveCount(0);
    await expect(beta.getByRole("button", { name: "They match" })).toBeVisible();

    // The second confirmation stores the peer on both, and the channel comes up.
    await beta.getByRole("button", { name: "They match" }).click();
    const row = (page: Page, id: string) => page.locator(`[data-testid="peer-row"][data-peer="${id}"]`);
    await expect(row(alpha, betaId)).toHaveAttribute("data-state", "connected", { timeout: 45_000 });
    await expect(row(beta, alphaId)).toHaveAttribute("data-state", "connected", { timeout: 45_000 });
    await expect(row(alpha, betaId)).toContainText("beta");
    await expect(row(beta, alphaId)).toContainText("alpha");
    await expect(row(alpha, betaId).getByTestId("peer-state")).toHaveText("connected");
    await expect(row(beta, alphaId).getByTestId("peer-state")).toHaveText("connected");
    await expect(alpha.getByTestId("peers-pairing")).toHaveCount(0);
    await expect(beta.getByTestId("peers-pairing")).toHaveCount(0);
  } catch (e) {
    console.error(`alpha:\n${pair.logs.alpha}\nbeta:\n${pair.logs.beta}`);
    throw e;
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});
