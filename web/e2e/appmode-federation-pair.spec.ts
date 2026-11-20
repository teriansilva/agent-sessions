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
import { remotePath } from "../src/lib/federation";

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

    // #2055, against the real routes: pairing shared nothing, in either direction (the real
    // `GET /api/federation/grants?peer=` and `GET /api/federation/remote`)...
    await expect(row(alpha, betaId).getByTestId("peer-shares-counts")).toHaveText(
      "Sessions · 0 shared · 0 received",
    );
    await expect(row(beta, alphaId).getByTestId("peer-shares-counts")).toHaveText(
      "Sessions · 0 shared · 0 received",
    );
    // ...and a share that does not exist, asked of the real receiving route for a real,
    // connected peer, is the neutral unavailable state with nothing shown.
    await beta.goto(`${pair.b.origin}${remotePath(alphaId, "g7Jx9aBcDeFgHiJkLmNoPq")}`);
    await expect(beta.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
    await expect(beta.getByTestId("remote-state")).toHaveText("unavailable");
    await expect(beta.locator("[data-turn]")).toHaveCount(0);

    // #2061, against the real routes. This harness cannot host an API session (an instance has
    // no agent runtime), so suggest → approve → the labelled turn is NOT shown here: that path's
    // evidence is `tests/test_federation_inputs.py`
    // (`test_two_real_instances_suggest_approve_send_and_revoke`). What two real servers CAN
    // show is asserted instead:
    // (1) the real grants route offers the three levels, never defaults to Send, and refuses
    //     Send without `accept_risk` BEFORE it looks at anything else (so the dialog's
    //     acknowledgement is not the only thing between an operator and a Send grant);
    const real = await alpha.evaluate(async (peerId) => {
      const config = (await (await fetch("/api/config")).json()) as { csrf: string };
      const list = await (await fetch(`/api/federation/grants?peer=${peerId}`)).json();
      const post = async (body: Record<string, unknown>) => {
        const r = await fetch("/api/federation/grants", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrf },
          body: JSON.stringify(body),
        });
        return { status: r.status, code: ((await r.json()) as { code?: string }).code };
      };
      const base = { session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f", peer_id: peerId };
      return {
        levels: list.levels,
        default_level: list.default_level,
        send_default: list.default_expires_in?.send,
        send_max: list.max_expires_in_by_level?.send,
        unticked: await post({ ...base, level: "send", expires_in: 3600 }),
        falsy: await post({ ...base, level: "send", expires_in: 3600, accept_risk: false }),
        tooLong: await post({ ...base, level: "send", expires_in: 86401, accept_risk: true }),
        after: (await (await fetch(`/api/federation/grants?peer=${peerId}`)).json()).grants.length,
      };
    }, betaId);
    expect(real.levels).toEqual(["view", "suggest", "send"]);
    expect(real.default_level).toBe("view");
    expect(real.send_default).toBe(3600); // the dialog's 1 hour
    expect(real.send_max).toBe(24 * 3600); // ...and its 24 hours
    expect(real.unticked).toEqual({ status: 422, code: "risk" });
    expect(real.falsy).toEqual({ status: 422, code: "risk" });
    expect(real.tooLong.status).toBe(422);
    expect(real.after).toBe(0);
    // (2) the real status route, asked by the real pane for a request "in flight before a
    //     reload" under a share that does not exist: the receiving server answers 404 at once
    //     (its mirror has no such share, so the owner is not asked; the owner's own refusal of
    //     `input.status` is pinned in `tests/test_federation_inputs.py`), and the pane of that
    //     share says "could not check", never "did not arrive", and resubmits nothing.
    const leftover = {
      peer_id: alphaId,
      grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
      client_key: "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d",
      kind: "send",
      started_at: Date.now(),
    };
    const writes: string[] = [];
    beta.on("request", (r) => {
      if (r.method() !== "GET" && /\/api\/federation\/remote\//.test(r.url())) writes.push(r.url());
    });
    const direct = await beta.evaluate(async (url) => (await fetch(url)).status, `/api/federation/remote/${alphaId}/${leftover.grant_id}/operations/${leftover.client_key}`);
    expect(direct).toBe(404);
    await beta.evaluate((op) => sessionStorage.setItem("bl-remote-ops", JSON.stringify([op])), leftover);
    await beta.reload();
    await expect(beta.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
    await expect(beta.getByTestId("remote-reload-notice")).toHaveText(
      "Could not check a message you were sending before the reload.",
    );
    await expect(beta.getByTestId("remote-pane")).not.toContainText(/not sent|did not (arrive|reach)/i);
    expect(writes).toEqual([]);

    // #2062, against the real routes. Still no API session, so no grant: the notes round trip,
    // an access request on a live grant, a peer's decision and Continue here's create are NOT
    // shown here (their two-instance evidence is `tests/test_federation_together.py`,
    // `tests/test_federation_notes.py` and `tests/test_federation_continue.py`). What two real
    // servers CAN show:
    // (1) the real pane of a share that does not exist offers none of the working-together
    //     controls: no ⋯ menu (no Notes, no Ask for access, no Continue here), no presence line;
    await expect(beta.getByTestId("remote-menu")).toHaveCount(0);
    await expect(beta.getByTestId("remote-access-ask")).toHaveCount(0);
    await expect(beta.getByTestId("remote-presence")).toHaveCount(0);
    await expect(beta.getByTestId("notes-drawer")).toHaveCount(0);
    // (2) every receiving route of #2059 / #2062 answers that share with the owner's one
    //     refusal (404 `no_grant`), and nothing is created on either side;
    const receiving = await beta.evaluate(async (base) => {
      const config = (await (await fetch("/api/config")).json()) as { csrf: string };
      const call = async (method: string, path: string, body?: Record<string, unknown>) => {
        const r = await fetch(`${base}/${path}`, {
          method,
          headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrf },
          body: body ? JSON.stringify(body) : undefined,
        });
        return [r.status, ((await r.json().catch(() => ({}))) as { code?: string }).code];
      };
      const key = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d";
      return {
        accessRead: await call("GET", "access"),
        ask: await call("POST", "access", { level: "send", note: "PLANTED request note" }),
        withdraw: await call("POST", "access/withdraw"),
        // (the route requires the snapshot's `offer` by shape before it asks the owner)
        decide: await call("POST", "decisions", {
          request_id: "req-1",
          offer: "o".repeat(32),
          choice: "approve",
          client_key: key,
        }),
        notesRead: await call("GET", "notes?after=0"),
        note: await call("POST", "notes", { client_key: key, text: "PLANTED note" }),
        prepare: await call("POST", "continue", { target_engine: "codex-api", mode: "quick" }),
      };
    }, `/api/federation/remote/${alphaId}/g7Jx9aBcDeFgHiJkLmNoPq`);
    for (const [name, answer] of Object.entries(receiving))
      expect(answer, name).toEqual([404, "no_grant"]);
    // (3) the owner's real reads: no access request, no grant to count notes for, and a thread
    //     of a grant that does not exist is refused, never an empty thread.
    const owner = await alpha.evaluate(async () => {
      const config = (await (await fetch("/api/config")).json()) as { csrf: string };
      const session = "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
      const requests = await (await fetch(`/api/federation/access-requests?session=${session}`)).json();
      const grants = await (await fetch(`/api/federation/grants?session=${session}`)).json();
      const thread = await fetch("/api/federation/grants/g7Jx9aBcDeFgHiJkLmNoPq/notes");
      const decline = await fetch("/api/federation/access-requests/r7Jx9aBcDeFgHiJkLmNoPq/decline", {
        method: "POST",
        headers: { "X-CSRF-Token": config.csrf },
      });
      return { requests, grants: grants.grants, thread: thread.status, decline: decline.status };
    });
    expect(owner.requests).toEqual({ access_requests: [] });
    expect(owner.grants).toEqual([]);
    expect(owner.thread).toBe(404);
    expect(owner.decline).toBe(404);
    // and nothing anyone typed above is in either browser's storage.
    for (const page of [alpha, beta])
      expect(await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]))).not.toContain(
        "PLANTED",
      );
  } catch (e) {
    console.error(`alpha:\n${pair.logs.alpha}\nbeta:\n${pair.logs.beta}`);
    throw e;
  } finally {
    await ctxA.close();
    await ctxB.close();
  }
});
