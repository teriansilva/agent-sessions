/** What the two-instance federation specs share (#2055, #2060, #2061, #2062): two operators'
 *  pages, paired through Settings → Peers, and the steps each journey starts from.
 *
 *  Every step is the page's own control unless its comment says otherwise. The one thing done
 *  through a route instead of a control is CREATING the owner's API session: the New-session
 *  dialog is not what these specs are about, so the page asks the app's real, authenticated
 *  `POST /api/structured/sessions` itself, with the CSRF token the app gave it. */
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

import type { FederationInstance, FederationPair } from "./federationPair";
import { openLinksFullScreen } from "./linkMode";
import { headAction, SHARE_PEER } from "./shareFixture";
import { SESSIONS_PATH } from "../src/lib/routes";
import { settingsPath } from "../src/routes/settingsTabs";

/** How long one instance may take to learn what the other did: the owner's hint, the receiving
 *  side's refresh and the page's own poll are each seconds apart. */
export const ACROSS_MS = 60_000;

export interface Operators {
  ctxA: BrowserContext;
  ctxB: BrowserContext;
  alpha: Page;
  beta: Page;
  /** Each instance's peer id, as its own Settings → Peers page shows it. */
  alphaId: string;
  betaId: string;
  close: () => Promise<void>;
}

/** Pair the two through Settings → Peers, as `appmode-federation-pair.spec.ts` proves in detail. */
export async function pairThroughThePages(
  pair: FederationPair,
  alpha: Page,
  beta: Page,
): Promise<{ alphaId: string; betaId: string }> {
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

/** Two operators, each in a browser context of their own, already paired. */
export async function twoOperators(browser: Browser, pair: FederationPair): Promise<Operators> {
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  const close = async () => {
    await ctxA.close();
    await ctxB.close();
  };
  try {
    // The "Full screen or map?" choice (#1232) is made, as the suite's storage state makes it for
    // the preview origin: these specs open session URLs on the instances' own origins.
    await openLinksFullScreen(ctxA);
    await openLinksFullScreen(ctxB);
    const alpha = await ctxA.newPage();
    const beta = await ctxB.newPage();
    const ids = await pairThroughThePages(pair, alpha, beta);
    return { ctxA, ctxB, alpha, beta, ...ids, close };
  } catch (e) {
    await close();
    throw e;
  }
}

/** Create a real API session on `instance` (the app's own create route, see the file comment),
 *  open it in `page`, and wait until its agent is ready for a message. Returns its pane. */
export async function openNewApiSession(page: Page, instance: FederationInstance): Promise<Locator> {
  await page.goto(`${instance.origin}${SESSIONS_PATH}`);
  const made = await page.evaluate(async (cwd) => {
    const config = (await (await fetch("/api/config")).json()) as { csrf: string };
    const r = await fetch("/api/structured/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrf },
      body: JSON.stringify({ engine: "codex-api", cwd, operation_id: crypto.randomUUID() }),
    });
    return { status: r.status, body: (await r.json()) as { session_key?: string } };
  }, instance.workdir);
  expect(made.status, JSON.stringify(made.body)).toBe(201);
  const native = made.body.session_key!.split(":")[1];
  await page.goto(`${instance.origin}/s/codex-api/${native}`);
  const pane = page.getByTestId("structured-pane");
  await expect(pane).toBeVisible({ timeout: 30_000 });
  return pane;
}

/** The operator of a local API session types a message and sends it. The scripted agent answers
 *  `echo:<text>`, or asks for permission first when the text is `APPROVE`. */
export async function say(pane: Locator, text: string): Promise<void> {
  const form = pane.getByRole("form", { name: "Compose message" });
  await form.locator("textarea").fill(text);
  await expect(pane.getByTestId("structured-send")).toBeEnabled({ timeout: 30_000 });
  await pane.getByTestId("structured-send").click();
}

/** The owner's "Share with a peer" dialog, opened from the session header's Share → Peer…. */
export async function openShareDialog(page: Page): Promise<Locator> {
  await headAction(page, SHARE_PEER);
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radiogroup", { name: "Peer" })).toBeVisible();
  return dialog;
}

export const accessLevel = (dialog: Locator, name: "View" | "Suggest" | "Send") =>
  dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio", { name: new RegExp(`^${name}`) });

/** Share the open session with beta at `level` through the dialog (ticking the risk box for
 *  Send, as only the operator can), and close the dialog. Sharing again changes the same grant. */
export async function shareWithBeta(page: Page, level: "View" | "Suggest" | "Send"): Promise<void> {
  const dialog = await openShareDialog(page);
  const tile = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio", { name: /beta/ });
  await expect(tile).toContainText("connected");
  await tile.click();
  await accessLevel(dialog, level).click();
  if (level === "Send") {
    await expect(dialog.getByTestId("share-peer-go")).toBeDisabled();
    await dialog.getByTestId("share-peer-ack").check();
  }
  await dialog.getByTestId("share-peer-go").click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText(`Shared with beta (${level})`);
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
}

/** The receiving operator's Shared tab (#2198) with the one row alpha shares, however long the
 *  owner's hint and the mirror's refresh take: the list is read when the page loads. */
export async function sharedRow(page: Page, instance: FederationInstance): Promise<Locator> {
  const row = page.getByTestId("remote-row");
  await expect(async () => {
    await page.goto(`${instance.origin}${SESSIONS_PATH}`);
    await page.getByTestId("scope-tab-shared").click({ timeout: 5_000 });
    await expect(row).toHaveCount(1, { timeout: 5_000 });
  }).toPass({ timeout: ACROSS_MS });
  return row;
}

/** Open the shared row: the receiving operator's pane of alpha's session. */
export async function openSharedPane(page: Page, instance: FederationInstance, alphaId: string): Promise<Locator> {
  const row = await sharedRow(page, instance);
  await expect(row.getByTestId("peer-badge")).toHaveText("alpha");
  await row.locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${alphaId}/[A-Za-z0-9_-]+$`));
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-state")).toHaveText("connected", { timeout: ACROSS_MS });
  return pane;
}

/** Both servers' logs, for a failure nobody can read from the page alone. */
export function dumpLogs(pair: FederationPair): void {
  console.error(`alpha:\n${pair.logs.alpha}\nbeta:\n${pair.logs.beta}`);
}

export interface AiStub {
  /** `http://127.0.0.1:<port>/v1`: what an instance is given as its AI endpoint. */
  baseUrl: string;
  /** Every chat completion asked of it, as the JSON body the app sent. */
  asked: { messages: { role: string; content: string }[] }[];
  stop: () => Promise<void>;
}

/** A model endpoint for the one journey that makes a model call (Continue here → Summary): a
 *  tiny HTTP server of this test's own. It listens on LOOPBACK ONLY (nothing outside this
 *  machine can reach it), keeps what it was asked and answers every chat completion with
 *  `summary` as the JSON object the brief's prompt asks for. No real endpoint is ever called. */
export async function startAiStub(summary: Record<string, unknown>): Promise<AiStub> {
  const asked: AiStub["asked"] = [];
  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const json = (body: unknown) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(body));
      };
      if (req.method === "POST" && req.url?.endsWith("/chat/completions")) {
        asked.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        json({ choices: [{ finish_reason: "stop", message: { role: "assistant", content: JSON.stringify(summary) } }] });
      } else if (req.method === "GET" && req.url?.endsWith("/models")) {
        json({ data: [{ id: "stub-model" }] });
      } else {
        res.writeHead(404).end();
      }
    });
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address() as AddressInfo;
  return {
    baseUrl: `http://127.0.0.1:${port}/v1`,
    asked,
    stop: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}

/** Point the instance `page` is on at `stub`, as Settings → AI saves an endpoint: the app's real
 *  `PATCH /api/prefs`, with the operator's acknowledgement that session text goes to it. The
 *  Settings page itself is not what these specs are about. */
export async function useAiStub(page: Page, stub: AiStub): Promise<void> {
  const saved = await page.evaluate(async (base_url) => {
    const config = (await (await fetch("/api/config")).json()) as { csrf: string };
    const r = await fetch("/api/prefs", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrf },
      body: JSON.stringify({
        ai_review: { egress_acknowledged: true, base_url, api_key: "stub-key", model: "stub-model" },
      }),
    });
    return { status: r.status, body: await r.text() };
  }, stub.baseUrl);
  expect(saved.status, saved.body).toBe(200);
}
