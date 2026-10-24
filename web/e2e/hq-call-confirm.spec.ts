/** Confirming by voice on a call (#1397 P2), in a real browser on desktop and mobile.
 *
 *  The server is mocked (`callFixture.ts`); each test plays the server's side: speech audio, then
 *  `said` behind it. The page must ack `played` only once that audio has PLAYED (Chromium's real
 *  audio clock), never for a say an interrupt flushed. Then the challenge shows on its card with a
 *  countdown, a tap closes it (`card_decided`), the settlement shows the route's answer, an
 *  operation that needs a choice says so, and "What needs me?" asks the server and shows the
 *  Needs-you rows it offers. */
import { expect, test, type Page } from "@playwright/test";

import { CID, FAKE_MIC, connect, mockShell, say, type Server } from "./callFixture";
import { TOUCH_FLOOR } from "./touchTarget";

test.use(FAKE_MIC);

/** `seconds` of quiet speech, PCM16 mono at 24 kHz. */
const speech = (seconds: number) => Buffer.alloc(Math.round(24_000 * seconds) * 2);

const sentOf = (srv: Server, t: string) =>
  srv.texts[0].map((m) => JSON.parse(m) as { t: string; id?: string }).filter((m) => m.t === t);

async function withCards(page: Page) {
  await page.route(/\/api\/ask\/conversations\/c{32}$/, (r) =>
    r.fulfill({
      json: {
        id: CID,
        title: "call",
        running: false,
        turns: [
          {
            turn_id: "t1",
            question: "Rerun the auth tests on api-rebase.",
            asked_at: 1,
            status: "done",
            answer: "Drafted.",
            detail: null,
            code: null,
            matches: [],
            mission_matches: [],
            tools: [],
            ask: null,
            contact: "asia-woman",
            proposals: [
              {
                action_id: "a1",
                kind: "nudge",
                key: "claude:00000000-0000-4000-8000-000000000001",
                title: "api-rebase",
                agent: "claude",
                text: "Rerun the auth tests and fix the two failures.",
                state: "pending",
                projection: "actionable",
                can_approve: true,
                can_reject: true,
                detail: "",
              },
              {
                action_id: "op1",
                kind: "operation",
                op: "project.move",
                risk: "reversible",
                label: "Move project",
                verb: "Move",
                title: "docs-site",
                text: "Move docs-site into another folder.",
                needs: ["folder"],
                state: "pending",
                summary: "",
                reason: "",
                can_confirm: true,
                can_dismiss: true,
                choices: { folder: ["/srv/a", "/srv/b"] },
              },
            ],
          },
        ],
      },
    }),
  );
}

async function onCall(page: Page) {
  const srv = await mockShell(page);
  await withCards(page);
  const panel = await connect(page, srv);
  srv.connected[0] = true;
  say(srv, { t: "connected" });
  await expect(panel).toHaveAttribute("data-phase", "connected");
  say(srv, { t: "turn", turn: { turn_id: "t1", status: "done" } });
  await expect(page.getByTestId("call-card")).toHaveCount(2);
  return { srv, panel };
}

test("the read-back is acked only after it played; the challenge, then the settlement, show on the card", async ({ page }) => {
  const { srv } = await onCall(page);
  const nudge = page.locator('[data-testid="call-card"][data-card="a1"]');
  const op = page.locator('[data-testid="call-card"][data-card="op1"]');
  // The operation needs a folder: never offered by voice.
  await expect(op.getByTestId("call-card-voice")).toHaveAttribute("data-voice", "screen");
  await expect(op).toContainText("Needs a choice on screen");

  srv.sockets[0].send(speech(1.2));
  say(srv, { t: "said", id: "rb1" });
  // Still playing: no ack yet.
  await page.waitForTimeout(500);
  expect(sentOf(srv, "played")).toEqual([]);
  // Played out: exactly one ack, for that say.
  await expect.poll(() => sentOf(srv, "played"), { timeout: 5_000 }).toEqual([{ t: "played", id: "rb1" }]);

  say(srv, { t: "challenge", cards: ["a1"], expires_s: 20 });
  const sayHint = nudge.getByTestId("call-card-say");
  await expect(sayHint).toHaveText(/^Say “confirm” · (20|19) s$/);
  await expect(nudge.getByTestId("call-card-voice")).toContainText("a tap ends the spoken window");

  // HQ heard "confirm": the challenge clears, the route's answer shows.
  say(srv, { t: "challenge", cards: [] });
  say(srv, { t: "settled", card: "a1", ok: true, status: 200 });
  await expect(nudge.getByTestId("call-card-voice")).toHaveAttribute("data-voice", "settled");
  await expect(nudge).toContainText("✓ Sent by voice");
  expect(sentOf(srv, "played")).toHaveLength(1);
});

test("an interrupt flushes the read-back: its marker is never acked", async ({ page }) => {
  const { srv } = await onCall(page);
  srv.sockets[0].send(speech(1.5));
  say(srv, { t: "said", id: "rb1" });
  await page.waitForTimeout(200);
  say(srv, { t: "interrupt" });
  // Later speech plays out; the flushed say still never acks.
  srv.sockets[0].send(speech(0.3));
  say(srv, { t: "said", id: "rb2" });
  await expect.poll(() => sentOf(srv, "played").map((m) => m.id), { timeout: 5_000 }).toEqual(["rb2"]);
  await page.waitForTimeout(1_800);
  expect(sentOf(srv, "played").map((m) => m.id)).toEqual(["rb2"]);
});

test("several cards say which; a tap during the window says card_decided and closes it", async ({ page }) => {
  const { srv } = await onCall(page);
  await page.route("**/api/pulse/actions/a1/approve", (r) => r.fulfill({ json: { ok: true } }));
  say(srv, { t: "challenge", cards: ["op1", "a1"], expires_s: 20 });
  const nudge = page.locator('[data-testid="call-card"][data-card="a1"]');
  await expect(nudge.getByTestId("call-card-say")).toHaveText(/^Say “confirm the second” · \d+ s$/);
  const send = nudge.getByTestId("ask-nudge-send");
  const box = (await send.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await send.click();
  await expect.poll(() => sentOf(srv, "card_decided")).toEqual([{ t: "card_decided", id: "a1" }]);
  await expect(page.getByTestId("call-card-say")).toHaveCount(0);
});

test("What needs me? asks the server and shows the Needs-you row it offers", async ({ page }) => {
  const { srv } = await onCall(page);
  await page.route(/\/api\/pulse\/needs-you(\?|$)/, (r) =>
    r.fulfill({
      json: {
        rows: [
          {
            id: "claude:00000000-0000-4000-8000-000000000002",
            engine: "claude",
            title: "deploy-check",
            project: { id: "p", name: "p" },
            last_activity: 1,
            since: 1,
            reason: "asks whether to retry the deploy",
            summary: "Retry the staging deploy?",
            flagged: false,
            kind: "question",
            menu: null,
            action: { id: "ny1", verb: "answer", state: "pending", can_approve: true, answer: "Yes, retry it." },
          },
        ],
        total: 1,
        total_unfiltered: 1,
        truncated: false,
        facets: { engines: [], projects: [] },
        window_days: 3,
      },
    }),
  );
  const chip = page.getByTestId("call-needs-you");
  await expect(chip).toHaveText("What needs me?");
  const box = (await chip.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await chip.click();
  await expect.poll(() => sentOf(srv, "needs_you")).toHaveLength(1);

  say(srv, { t: "challenge", cards: ["ny1"], expires_s: 20 });
  const row = page.locator('[data-testid="call-card"][data-card="ny1"]');
  await expect(row.getByTestId("call-needs-you-card")).toContainText("Yes, retry it.");
  await expect(row.getByTestId("call-card-say")).toHaveText(/^Say “confirm” · \d+ s$/);
  say(srv, { t: "challenge", cards: [] });
  say(srv, { t: "settled", card: "ny1", ok: true, status: 200 });
  await expect(row).toContainText("✓ Sent by voice");
  await expect(row.getByTestId("call-needs-you-card")).toHaveCount(0);
});
