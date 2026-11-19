import { expect, test, type Locator, type Page } from "@playwright/test";
import { clickHeadAction } from "./headActions";
import { animationsSettled } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";
import {
  CSRF,
  DAY,
  FakeShare,
  GONE,
  GPU,
  GRANT_A,
  GRANT_B,
  GRANT_T,
  ID,
  KEY,
  NOW,
  OFFLINE,
  RENDER,
  RENDER_TURNS,
  SHARE_PEER,
  TERM_KEY,
  TERM_TITLE,
  TERM_URL,
  TITLE,
  URL_PATH,
  controlsAtLeast44,
  gate,
  headAction,
  isMobile,
  localTurn,
  noHorizontalOverflow,
  openPane,
  painted,
  setup,
  setupTerminal,
  showList,
  showShared,
  snapshot,
  turn,
  type Answer,
  type Json,
} from "./shareFixture";

// A paired instance SUGGESTS or SENDS messages to a shared API session (#1246 P5, #2061), in a
// real browser on both projects, against the in-test model of `routes/federation.py`
// (`shareFixture.ts`). Three surfaces: the owner's dialog (the levels, the Send risk and its
// acknowledgement), the owner's pane (the suggestion cards, attribution), and the receiving
// pane (the composers, delivery states, a reload in the middle of a request, the fence).
//
// What is asserted here is what only a browser shows: that Share is really disabled, that
// hostile text stays characters, that a late answer changes nothing on screen, that storage
// holds no message text, and that nothing is ever submitted a second time on its own.

const SEND_PATH = `/remote/${GPU}/${GRANT_A}/turns`;
const SUGGEST_PATH = `/remote/${GPU}/${GRANT_A}/suggest`;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const RISK =
  "Send lets gpu-box's operator instruct this agent directly. Whatever this agent can do on this machine, their messages can ask it to do.";
const UNKNOWN_NOTICE =
  "A message you were sending before the reload may or may not have arrived. Check the conversation before sending it again.";
const UNCHECKED_NOTICE = "Could not check a message you were sending before the reload.";
const OPS_KEY = "bl-remote-ops";

// ---- helpers ------------------------------------------------------------------------------------

async function ownerDialog(page: Page): Promise<Locator> {
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await headAction(page, SHARE_PEER);
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radiogroup", { name: "Peer" })).toBeVisible();
  await animationsSettled(page);
  return dialog;
}
const level = (dialog: Locator, name: string) =>
  dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio", { name: new RegExp(`^${name}`) });
const shareButton = (dialog: Locator) => dialog.getByTestId("share-peer-go");
const ack = (dialog: Locator) => dialog.getByTestId("share-peer-ack");
const expiries = (dialog: Locator) =>
  dialog.getByRole("radiogroup", { name: "Expires" }).getByRole("radio").allInnerTexts();
const grantWrites = (fake: FakeShare) =>
  fake.writes.filter((w) => w.path === "/grants" || /^\/grants\/[^/]+$/.test(w.path));

async function ownerPane(page: Page): Promise<Locator> {
  await page.goto(URL_PATH);
  const pane = page.getByTestId("structured-pane");
  await expect(pane).toBeVisible();
  return pane;
}
const cards = (page: Page) => page.getByTestId("peer-suggestion");
const decisions = (fake: FakeShare, verb?: "approve" | "reject") =>
  fake.writes.filter((w) => new RegExp(`^/suggestions/[^/]+/${verb ?? "(approve|reject)"}$`).test(w.path));

/** The receiving side with `GRANT_A` at `level`; the conversation is what `convo` holds NOW. */
interface Convo {
  turns: Json[];
  revision: number;
  mode: "live" | "offline" | "gone";
}
async function inputSetup(
  page: Page,
  level_: "view" | "suggest" | "send",
  prepare?: (f: FakeShare, convo: Convo) => void,
  over: Json = {},
): Promise<{ fake: FakeShare; convo: Convo }> {
  const convo: Convo = { turns: RENDER_TURNS, revision: 5, mode: "live" };
  const fake = await setup(page, (f) => {
    f.remote[0] = { ...f.remote[0], level: level_, ...over };
    f.remote[1] = { ...f.remote[1], level: level_ };
    f.onRead = (kind, grant) => {
      if (grant !== GRANT_A)
        return kind === "snapshot"
          ? { json: snapshot([turn("bbbbbbbb-0000-4000-8000-000000000001", "Which LUT is in use?", "ACES 1.3.")]) }
          : kind === "events"
            ? { json: { link: "L".repeat(24), revision: 5, next_cursor: 5, events: [] } }
            : GONE;
      if (convo.mode === "offline") return OFFLINE;
      if (convo.mode === "gone") return GONE;
      if (kind === "snapshot")
        return { json: snapshot(convo.turns, { revision: convo.revision, event_cursor: convo.revision }) };
      if (kind === "events")
        return { json: { link: "L".repeat(24), revision: convo.revision, next_cursor: convo.revision, events: [] } };
      return GONE; // (the default answers `suggestions` from `fake.own`)
    };
    prepare?.(f, convo);
  });
  return { fake, convo };
}
const box = (pane: Locator) => pane.getByTestId("remote-composer-text");
const go = (pane: Locator) => pane.getByTestId("remote-composer-go");
const writes = (fake: FakeShare, path: string) => fake.writes.filter((w) => w.path === path);
const keys = (fake: FakeShare, path: string) => writes(fake, path).map((w) => String(w.body?.client_key));
const stored = (page: Page) =>
  page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]));
const records = (page: Page) =>
  page.evaluate((k) => JSON.parse(sessionStorage.getItem(k) ?? "[]") as Record<string, unknown>[], OPS_KEY);

async function say(pane: Locator, text: string) {
  await box(pane).fill(text);
  await go(pane).click();
}
/** Nothing more is submitted: the count holds while the page had every chance to do it. */
async function neverAgain(page: Page, count: () => number, expected: number, ms = 1_500) {
  await page.waitForTimeout(ms);
  expect(count()).toBe(expected);
}

// =================================================================================================
// The owner: granting
// =================================================================================================

test("Send cannot be shared without the acknowledgement, and accept_risk travels only after the tick", async ({
  page,
}) => {
  const fake = await setup(page);
  const dialog = await ownerDialog(page);
  await level(dialog, "Send").click();
  // the Send expiries are 1 h (default), 4 h, 24 h
  expect(await expiries(dialog)).toEqual(["1 HOUR", "4 HOURS", "24 HOURS"].map((x) => expect.stringMatching(new RegExp(x, "i"))));
  await expect(dialog.getByRole("radiogroup", { name: "Expires" }).getByRole("radio", { name: "1 hour" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await expect(dialog.getByTestId("share-peer-hint")).toHaveText("It ends by itself after 1 hour. Only you can renew it.");
  // the risk box: the one red edge, with its word and the exact text
  const risk = dialog.getByTestId("share-peer-risk");
  await expect(risk).toContainText(RISK);
  await expect(risk.locator("span").first()).toHaveText(/risk/i);
  const edge = await risk.evaluate((el) => getComputedStyle(el).borderLeftColor);
  const down = await page.evaluate(() => {
    const probe = document.createElement("i");
    probe.style.color = "var(--status-down)";
    document.body.append(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  });
  expect(edge).toBe(down);
  await expect(ack(dialog)).not.toBeChecked();
  await expect(dialog.getByText("I understand and want to allow this.")).toBeVisible();

  // Share is DISABLED: not merely inert. A forced click and Enter make no request either.
  await expect(shareButton(dialog)).toBeDisabled();
  await shareButton(dialog).click({ force: true });
  await shareButton(dialog).dispatchEvent("click");
  await page.keyboard.press("Enter");
  await dialog.getByRole("radio", { name: "4 hours", exact: true }).click();
  await expect(shareButton(dialog)).toBeDisabled(); // an expiry is not an acknowledgement
  await neverAgain(page, () => grantWrites(fake).length, 0, 500);

  await ack(dialog).check();
  await expect(shareButton(dialog)).toBeEnabled();
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("Shared with gpu-box (Send)");
  expect(grantWrites(fake)).toHaveLength(1);
  expect(grantWrites(fake)[0]).toMatchObject({
    method: "POST",
    path: "/grants",
    csrf: CSRF,
    body: { session_key: KEY, peer_id: GPU, level: "send", expires_in: 4 * 3600, accept_risk: true },
  });
  // the grant is listed at its level with a live countdown, and the tick is spent
  const row = dialog.getByTestId("share-peer-grant");
  await expect(row).toContainText("gpu-box");
  await expect(row).toContainText(/send/i);
  await expect(row.getByTestId("share-peer-until")).toHaveText(/^ends in (3 h 59 min|4 h)$/);
  await expect(ack(dialog)).not.toBeChecked();
  await expect(shareButton(dialog)).toBeDisabled();
});

test("View and Suggest never carry accept_risk; Suggest has its expiries and its line", async ({ page }) => {
  const fake = await setup(page);
  const dialog = await ownerDialog(page);
  // tick at Send, then leave Send: the tick must not ride along
  await level(dialog, "Send").click();
  await ack(dialog).check();
  await level(dialog, "Suggest").click();
  expect((await expiries(dialog)).map((x) => x.toLowerCase())).toEqual(["1 day", "7 days", "30 days"]);
  await expect(dialog.getByRole("radio", { name: "7 days" })).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByTestId("share-peer-suggest-line")).toHaveText(
    "A suggestion reaches the agent only after you approve that one message. Nothing approves it for you.",
  );
  await expect(dialog.getByTestId("share-peer-risk")).toHaveCount(0);
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("(Suggest)");
  await dialog.getByRole("radio", { name: /render-box/ }).click();
  await level(dialog, "View").click();
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(2);
  const sent = grantWrites(fake).map((w) => w.body);
  expect(sent).toEqual([
    { session_key: KEY, peer_id: GPU, level: "suggest", expires_in: 7 * DAY },
    { session_key: KEY, peer_id: RENDER, level: "view", expires_in: 7 * DAY },
  ]);
  for (const body of sent) expect(body).not.toHaveProperty("accept_risk");
});

test("Send is never preselected, also when the dialog is reopened for a session that has a Send grant", async ({
  page,
}) => {
  const fake = await setup(page, (f) => {
    f.grants.push(f.grant(GPU, { level: "send", expires_at: NOW() + 42 * 60 + 30 }));
    f.grants.push(f.grant(RENDER, { level: "suggest" }));
  });
  for (const round of [1, 2]) {
    const dialog = await ownerDialog(page);
    await expect(level(dialog, "View"), `round ${round}`).toHaveAttribute("aria-checked", "true");
    await expect(level(dialog, "Send")).toHaveAttribute("aria-checked", "false");
    await expect(level(dialog, "Suggest")).toHaveAttribute("aria-checked", "false");
    await expect(dialog.getByTestId("share-peer-risk")).toHaveCount(0);
    // existing shares: their level, and a live countdown for the one ending within a day
    const rows = dialog.getByTestId("share-peer-grant");
    await expect(rows.filter({ hasText: "gpu-box" }).getByTestId("share-peer-until")).toHaveText(/^ends in 4[12] min$/);
    await expect(rows.filter({ hasText: "gpu-box" })).toContainText(/send/i);
    await expect(rows.filter({ hasText: "render-box" })).toContainText(/suggest/i);
    await expect(rows.filter({ hasText: "render-box" }).getByTestId("share-peer-until")).toHaveText(/^until /);
    // select Send and tick, then close: the next opening starts from View, unticked
    await level(dialog, "Send").click();
    await ack(dialog).check();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
  }
  expect(grantWrites(fake)).toHaveLength(0);
});

test("the acknowledgement unticks on changing peer, changing level and reopening", async ({ page }) => {
  const fake = await setup(page);
  let dialog = await ownerDialog(page);
  await level(dialog, "Send").click();
  await ack(dialog).check();
  await expect(shareButton(dialog)).toBeEnabled();

  await dialog.getByRole("radio", { name: /render-box/ }).click(); // another peer
  await expect(ack(dialog)).not.toBeChecked();
  await expect(shareButton(dialog)).toBeDisabled();
  await expect(dialog.getByTestId("share-peer-risk")).toContainText("Send lets render-box's operator");

  await ack(dialog).check();
  await level(dialog, "Suggest").click(); // another level
  await level(dialog, "Send").click(); // and back
  await expect(ack(dialog)).not.toBeChecked();
  await expect(shareButton(dialog)).toBeDisabled();

  await ack(dialog).check();
  await expect(shareButton(dialog)).toBeEnabled();
  await page.keyboard.press("Escape"); // closed...
  await expect(dialog).toBeHidden();
  await headAction(page, SHARE_PEER); // ...and reopened, without a reload
  dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(level(dialog, "View")).toHaveAttribute("aria-checked", "true");
  await level(dialog, "Send").click();
  await expect(ack(dialog)).not.toBeChecked();
  await expect(shareButton(dialog)).toBeDisabled();
  expect(grantWrites(fake)).toHaveLength(0);
});

test("a mission-held session offers only View", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.mission = true;
  });
  const dialog = await ownerDialog(page);
  await expect(level(dialog, "View")).toHaveAttribute("aria-checked", "true");
  await expect(level(dialog, "Suggest")).toBeDisabled();
  await expect(level(dialog, "Send")).toBeDisabled();
  await expect(dialog.getByTestId("share-peer-held")).toHaveText(
    "This session belongs to a mission right now. It can only be watched.",
  );
  await level(dialog, "Send").click({ force: true });
  await expect(level(dialog, "Send")).toHaveAttribute("aria-checked", "false");
  await expect(dialog.getByTestId("share-peer-risk")).toHaveCount(0);
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("(View)");
  expect(grantWrites(fake).map((w) => w.body?.level)).toEqual(["view"]);
});

// The same dialog from the session list (#2173): the row's ⋯ menu, Share → Peer…. The levels and
// the mission rule are the dialog's, whichever entry opened it.
async function rowDialog(page: Page): Promise<Locator> {
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  const list = await showList(page);
  const row = list.getByRole("listitem").filter({ hasText: TITLE });
  await expect(row).toBeVisible();
  if (!isMobile(page)) await row.hover();
  await row.getByRole("button", { name: "Session actions" }).click();
  const menu = page.getByRole("menu", { name: "Session actions" });
  await expect(menu).toBeVisible();
  await animationsSettled(page);
  await menu.getByRole("menuitem", { name: SHARE_PEER }).click();
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radiogroup", { name: "Peer" })).toBeVisible();
  await animationsSettled(page);
  return dialog;
}

test("opened from the row's ⋯ menu the dialog offers the three levels, and Send still needs its tick", async ({
  page,
}) => {
  const fake = await setup(page);
  const dialog = await rowDialog(page);
  await expect(level(dialog, "View")).toHaveAttribute("aria-checked", "true");
  await level(dialog, "Send").click();
  await expect(dialog.getByTestId("share-peer-risk")).toBeVisible();
  await expect(ack(dialog)).not.toBeChecked();
  await expect(shareButton(dialog)).toBeDisabled();
  await level(dialog, "Suggest").click();
  await expect(dialog.getByTestId("share-peer-risk")).toHaveCount(0);
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("(Suggest)");
  expect(grantWrites(fake).map((w) => w.body)).toEqual([
    expect.objectContaining({ session_key: KEY, level: "suggest" }),
  ]);
  expect(grantWrites(fake)[0].body).not.toHaveProperty("accept_risk");
});

test("opened from the row's ⋯ menu a mission-held session offers only View", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.mission = true;
  });
  const dialog = await rowDialog(page);
  await expect(level(dialog, "View")).toHaveAttribute("aria-checked", "true");
  await expect(level(dialog, "Suggest")).toBeDisabled();
  await expect(level(dialog, "Send")).toBeDisabled();
  await expect(dialog.getByTestId("share-peer-held")).toHaveText(
    "This session belongs to a mission right now. It can only be watched.",
  );
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("(View)");
  expect(grantWrites(fake).map((w) => w.body?.level)).toEqual(["view"]);
});

test("a 503 busy on Revoke shows the retry state and no success; so does one on a change", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.grants.push(f.grant(GPU, { level: "send", expires_at: NOW() + 1800 }));
    f.refuseCoded[`POST /grants/${f.grants[0].grant_id}/revoke`] = {
      status: 503,
      json: { detail: "a delivery under this grant is in flight — try again", code: "busy" },
    };
  });
  const id = fake.grants[0].grant_id;
  const dialog = await ownerDialog(page);
  await dialog.getByRole("button", { name: "Revoke gpu-box" }).click();
  const stuck = dialog.getByTestId("share-peer-busy");
  await expect(stuck).toContainText("A message is being delivered. Nothing changed. Try again.");
  // never shown as success: no done line, the grant is still listed, and nothing says revoked
  await expect(dialog.getByTestId("share-peer-done")).toHaveCount(0);
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1);
  await expect(dialog.getByRole("button", { name: "Revoke gpu-box" })).toBeEnabled();
  // amber (it needs the operator), not the ok rule and not red
  const tone = await stuck.evaluate((el) => getComputedStyle(el).borderLeftColor);
  const [degraded, up] = await page.evaluate(() =>
    ["--status-degraded", "--status-up"].map((v) => {
      const probe = document.createElement("i");
      probe.style.color = `var(${v})`;
      document.body.append(probe);
      const c = getComputedStyle(probe).color;
      probe.remove();
      return c;
    }),
  );
  expect(tone).toBe(degraded);
  expect(tone).not.toBe(up);

  await stuck.getByRole("button", { name: "Try again" }).click(); // the fence is free now
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(0);
  await expect(stuck).toHaveCount(0);
  expect(fake.writes.filter((w) => w.path === `/grants/${id}/revoke`)).toHaveLength(2);

  // a change (sharing again with a peer that has a grant) refused the same way
  fake.grants.push(fake.grant(RENDER, { level: "view" }));
  const other = fake.grants[fake.grants.length - 1].grant_id;
  fake.refuseCoded[`PATCH /grants/${other}`] = { status: 503, json: { detail: "busy", code: "busy" } };
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await headAction(page, SHARE_PEER);
  await dialog.getByRole("radio", { name: /render-box/ }).click();
  await level(dialog, "Suggest").click();
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-busy")).toContainText("A message is being delivered. Nothing changed. Try again.");
  await expect(dialog.getByTestId("share-peer-done")).toHaveCount(0);
  await expect(dialog.getByTestId("share-peer-grant")).toContainText(/view/i); // nothing changed
  await dialog.getByTestId("share-peer-retry").click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("(Suggest)");
});

test("on a phone the Send sheet keeps Share disabled until the checkbox is ticked, and both are reachable", async ({
  page,
}) => {
  test.skip(!isMobile(page), "the sheet is the ≤800px form");
  await page.setViewportSize({ width: 390, height: 700 });
  const fake = await setup(page, (f) => {
    for (const [i, p] of [GPU, RENDER].entries()) f.grants.push(f.grant(p, { grant_id: `grant${i}`.padEnd(22, "x") }));
  });
  const dialog = await ownerDialog(page);
  await level(dialog, "Send").click();
  const share = shareButton(dialog);
  // Share is pinned at the sheet's floor: on screen before and after scrolling, and disabled
  await expect(share).toBeInViewport({ ratio: 1 });
  await expect(share).toBeDisabled();
  const body = dialog.getByTestId("share-peer-body");
  await body.evaluate((el) => (el.scrollTop = 0));
  await expect(share).toBeInViewport({ ratio: 1 });
  await expect(share).toBeDisabled();
  // the checkbox is reached by scrolling the BODY (the page never scrolls sideways or away)
  await ack(dialog).scrollIntoViewIfNeeded();
  await expect(ack(dialog)).toBeInViewport({ ratio: 1 });
  await expect(share).toBeInViewport({ ratio: 1 });
  const [checkbox, button] = [(await ack(dialog).boundingBox())!, (await share.boundingBox())!];
  expect(checkbox.y + checkbox.height).toBeLessThanOrEqual(button.y + 1); // not under the pinned row
  await expect(dialog.getByTestId("share-peer-risk")).toContainText(RISK);
  await noHorizontalOverflow(page);
  await controlsAtLeast44(dialog);
  await ack(dialog).tap();
  await expect(ack(dialog)).toBeChecked();
  await expect(share).toBeEnabled();
  await share.tap();
  await expect.poll(() => grantWrites(fake).length).toBe(1);
  expect(grantWrites(fake)[0].body).toMatchObject({ level: "send", accept_risk: true });
});

// =================================================================================================
// The owner: suggestions
// =================================================================================================

const HOSTILE =
  '<img src="https://evil.example/pixel.png" onerror="window.__pwned=1"><script>window.__pwned=2</script>' +
  "\n[click me](javascript:window.__pwned=3) **bold** _em_ `code`\n# heading\nhttps://evil.example/phish";

test("a suggestion card renders hostile text (markup, a link, Markdown) as plain text", async ({ page }) => {
  const asked: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("evil.example")) asked.push(r.url());
  });
  await setup(page, (f) => {
    f.suggestions = [f.suggestion({ text: HOSTILE, peer_name: "<b>gpu</b>-box" })];
  });
  await ownerPane(page);
  const card = cards(page);
  await expect(card).toHaveCount(1);
  const text = card.getByTestId("peer-suggestion-text");
  // every character as it was written, line breaks kept
  expect(await text.evaluate((el) => el.textContent)).toBe(HOSTILE);
  expect(await text.evaluate((el) => getComputedStyle(el).whiteSpace)).toBe("pre-wrap");
  // and nothing of it became an element: no markup, no Markdown, no link
  expect(await text.evaluate((el) => el.childElementCount)).toBe(0);
  for (const tag of ["img", "script", "a", "strong", "b", "em", "code", "h1", "iframe"])
    await expect(card.locator(tag), tag).toHaveCount(0);
  await expect(card.getByTestId("peer-badge")).toHaveText("<b>gpu</b>-box"); // the name too
  // the edit box holds the same characters
  await card.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(card.getByTestId("peer-suggestion-edit")).toHaveValue(HOSTILE);
  await painted(page);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  expect(asked).toEqual([]);
});

test("Approve, Edit then Approve edited, and Reject each call the right route once", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.localTurns = [localTurn("aaaaaaaa-0000-4000-8000-0000000000a1", "Make the importer stream.", "Parsing and validation are split now.")];
    f.suggestions = [
      f.suggestion({ text: "Keep the old batch entry point." }),
      f.suggestion({ text: "Log the row number on failure?" }),
      f.suggestion({ text: "Drop the retry limit." }),
    ];
    f.grants.push(f.grant(GPU, { level: "suggest" }));
  });
  const [a, b, c] = fake.suggestions.map((x) => String(x.suggestion_id));
  const pane = await ownerPane(page);
  await expect(cards(page)).toHaveCount(3);
  // above the composer, oldest first
  const zone = page.getByTestId("peer-suggestions");
  const composer = pane.getByRole("form", { name: "Compose message" });
  expect((await zone.boundingBox())!.y).toBeLessThan((await composer.boundingBox())!.y);
  await expect(cards(page).first().getByTestId("peer-suggestion-text")).toHaveText("Keep the old batch entry point.");
  await expect(zone.getByTestId("peer-suggestion-note")).toHaveText(
    "Nothing is sent until you approve. It is not shown to the agent.",
  );
  await expect(cards(page).first().getByTestId("peer-suggestion-word")).toHaveText(/waiting/i);

  // Approve: one call, no text; the turn arrives labelled with the peer
  await cards(page).first().getByRole("button", { name: "Approve", exact: true }).click();
  await expect(cards(page)).toHaveCount(2);
  expect(decisions(fake)).toHaveLength(1);
  expect(decisions(fake)[0]).toMatchObject({ method: "POST", path: `/suggestions/${a}/approve`, body: {}, csrf: CSRF });
  const turns = pane.getByTestId("structured-turn");
  await expect(turns).toHaveCount(2, { timeout: 10_000 });
  await expect(turns.nth(1).getByTestId("peer-attribution")).toHaveText(/gpu-box\s*·\s*peer/i);
  await expect(turns.nth(1).getByTestId("peer-edited")).toHaveCount(0);
  await expect(turns.nth(1)).toContainText("Keep the old batch entry point.");

  // Edit, then Approve edited: one call, with the changed text; the turn says so
  const second = cards(page).first();
  await second.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(second.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  await second.getByRole("button", { name: "Cancel" }).click(); // Cancel restores the card
  await expect(second.getByTestId("peer-suggestion-text")).toHaveText("Log the row number on failure?");
  await second.getByRole("button", { name: "Edit", exact: true }).click();
  await second.getByTestId("peer-suggestion-edit").fill("Log the row number when a row fails. One line.");
  await second.getByRole("button", { name: "Approve edited" }).click();
  await expect(cards(page)).toHaveCount(1);
  expect(decisions(fake)).toHaveLength(2);
  expect(decisions(fake)[1]).toMatchObject({
    path: `/suggestions/${b}/approve`,
    body: { text: "Log the row number when a row fails. One line." },
  });
  await expect(turns).toHaveCount(3, { timeout: 10_000 });
  await expect(turns.nth(2).getByTestId("peer-edited")).toHaveText(/edited by you/i);

  // Reject: one call, to the reject route
  await cards(page).first().getByRole("button", { name: "Reject" }).click();
  await expect(cards(page)).toHaveCount(0);
  expect(decisions(fake)).toHaveLength(3);
  expect(decisions(fake)[2]).toMatchObject({ method: "POST", path: `/suggestions/${c}/reject` });
  expect(decisions(fake, "approve")).toHaveLength(2);
  await expect(turns).toHaveCount(3);
  await neverAgain(page, () => decisions(fake).length, 3, 1_000);
});

for (const [name, answer, now, shown] of [
  [
    "the peer withdrew it first",
    { code: "conflict", state: "withdrawn" },
    { state: "withdrawn", end_reason: "peer" },
    "gpu-box withdrew this suggestion.",
  ],
  [
    "the share ended",
    { code: "ended", state: "pending" },
    { state: "withdrawn", end_reason: "grant_revoked" },
    "This suggestion was withdrawn because the share ended.",
  ],
  ["another tab approved it", { code: "conflict", state: "approved" }, { state: "approved" }, "Already approved."],
] as const) {
  test(`a 409 shows the server's outcome: ${name}`, async ({ page }) => {
    const fake = await setup(page, (f) => {
      f.suggestions = [f.suggestion()];
      f.onDecide = (id) => {
        Object.assign(f.suggestions.find((x) => x.suggestion_id === id)!, now);
        return { status: 409, json: { detail: "this suggestion is already decided", ...answer } };
      };
    });
    await ownerPane(page);
    await cards(page).getByRole("button", { name: "Approve", exact: true }).click();
    const card = cards(page);
    await expect(card.getByTestId("peer-suggestion-outcome")).toHaveText(shown);
    await expect(card.getByRole("button")).toHaveCount(0); // nothing left to decide
    await expect(card.getByTestId("peer-suggestion-word")).not.toHaveText(/waiting/i);
    expect(decisions(fake)).toHaveLength(1);
    // it leaves after a moment
    await expect(card).toHaveCount(0, { timeout: 12_000 });
    expect(decisions(fake)).toHaveLength(1);
  });
}

test("two waiting show the count, and no control or key approves more than one", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.suggestions = [f.suggestion({ text: "First suggestion." }), f.suggestion({ text: "Second suggestion." })];
  });
  await ownerPane(page);
  await expect(cards(page)).toHaveCount(2);
  const zone = page.getByTestId("peer-suggestions");
  await expect(zone.getByTestId("peer-suggestions-count")).toHaveText(/^2 waiting$/i);
  // every control in the zone is one card's own: Approve · Edit · Reject, twice. Nothing else.
  expect((await zone.getByRole("button").allInnerTexts()).map((x) => x.toLowerCase())).toEqual([
    "approve",
    "edit",
    "reject",
    "approve",
    "edit",
    "reject",
  ]);
  await expect(zone.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /approve all|all suggestions|always/i })).toHaveCount(0);
  await expect(page.getByText(/always approve|approve all/i)).toHaveCount(0);
  // nothing on the page offers to pass a suggestion on to HQ or an assistant
  await expect(zone.getByRole("link")).toHaveCount(0);
  await expect(zone.getByText(/ask hq|assistant|forward/i)).toHaveCount(0);

  // no key approves while focus is not on a card's Approve button
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  for (const key of ["Enter", "Space", "a", "y", "Control+Enter", "Meta+Enter", "Shift+Enter"]) await page.keyboard.press(key);
  await zone.getByTestId("peer-suggestion-text").first().click();
  await page.keyboard.press("Enter");
  await neverAgain(page, () => decisions(fake).length, 0, 800);

  // one Approve decides exactly one
  await cards(page).first().getByRole("button", { name: "Approve", exact: true }).click();
  await expect(cards(page)).toHaveCount(1);
  expect(decisions(fake).map((w) => w.path)).toEqual([`/suggestions/${fake.suggestions[0].suggestion_id}/approve`]);
  await expect(cards(page).getByTestId("peer-suggestion-text")).toHaveText("Second suggestion.");
  await expect(zone.getByTestId("peer-suggestions-count")).toHaveCount(0);
  await neverAgain(page, () => decisions(fake).length, 1, 800);
});

test("each suggestion state on the owner: approving is not delivered, expired is not rejected", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.suggestions = [
      f.suggestion({ text: "One." }),
      f.suggestion({ text: "Two." }),
      f.suggestion({ text: "Three." }),
      f.suggestion({ text: "Four." }),
      // settled long ago: never a card
      f.suggestion({ text: "Old approved.", state: "approved" }),
      f.suggestion({ text: "Old rejected.", state: "rejected" }),
      f.suggestion({ text: "Old withdrawn.", state: "withdrawn", end_reason: "peer" }),
    ];
  });
  await ownerPane(page);
  await expect(cards(page)).toHaveCount(4);
  await expect(cards(page).locator('[data-testid="peer-suggestion-word"]')).toHaveText([/waiting/i, /waiting/i, /waiting/i, /waiting/i]);
  await expect(page.getByText("Old approved.")).toHaveCount(0);
  await expect(page.getByText("Old withdrawn.")).toHaveCount(0);

  // the other side, time and another tab move them; the pane's own poll shows each
  Object.assign(fake.suggestions[0], { state: "approving", approved_text: "One." });
  Object.assign(fake.suggestions[1], { state: "withdrawn", end_reason: "peer" });
  Object.assign(fake.suggestions[2], { state: "expired", end_reason: "expired" });
  Object.assign(fake.suggestions[3], { state: "withdrawn", end_reason: "grant_expired" });

  const approving = page.locator('[data-testid="peer-suggestion"][data-state="approving"]');
  await expect(approving).toHaveCount(1, { timeout: 10_000 });
  await expect(approving.getByTestId("peer-suggestion-approving")).toHaveText("Approved · delivering…");
  await expect(approving.getByRole("button")).toHaveCount(0);
  await expect(approving).not.toContainText(/delivered|\bsent\b/i);

  const withdrawn = page.locator('[data-testid="peer-suggestion"][data-state="withdrawn"]');
  await expect(withdrawn.getByTestId("peer-suggestion-outcome")).toHaveText([
    "gpu-box withdrew this suggestion.",
    "This suggestion was withdrawn because the share ended.",
  ]);
  const expired = page.locator('[data-testid="peer-suggestion"][data-state="expired"]');
  await expect(expired.getByTestId("peer-suggestion-outcome")).toHaveText("This suggestion expired.");
  await expect(expired).not.toContainText(/reject/i);
  // grey, not amber: they no longer need the operator
  for (const quiet of [withdrawn.first(), expired, approving]) {
    const edge = await quiet.evaluate((el) => getComputedStyle(el).borderLeftColor);
    const amber = await cards(page).first().evaluate(() => {
      const probe = document.createElement("i");
      probe.style.color = "var(--status-degraded)";
      document.body.append(probe);
      const c = getComputedStyle(probe).color;
      probe.remove();
      return c;
    });
    expect(edge).not.toBe(amber);
  }
  // the notes leave; "approving" stays until the delivery is known
  await expect(withdrawn).toHaveCount(0, { timeout: 12_000 });
  await expect(expired).toHaveCount(0);
  await expect(approving).toHaveCount(1);
  // delivered: the card leaves (the turn carries its own state)
  Object.assign(fake.suggestions[0], { state: "approved", handoff: "sent" });
  await expect(cards(page)).toHaveCount(0, { timeout: 10_000 });
  expect(decisions(fake)).toHaveLength(0);
});

test("a peer-authored turn carries the label in the local pane, and an edited one says so", async ({ page }) => {
  await setup(page, (f) => {
    f.localTurns = [
      localTurn("aaaaaaaa-0000-4000-8000-0000000000a1", "Make the importer stream.", "Split now."),
      localTurn("aaaaaaaa-0000-4000-8000-0000000000a2", "Keep the old batch entry point.", "Kept as a wrapper.", {
        author: { name: "gpu-box", via: "peer" },
      }),
      localTurn("aaaaaaaa-0000-4000-8000-0000000000a3", "Log the row number. One line.", "Done.", {
        author: { name: "<i>gpu</i>-box", via: "peer", edited: true },
      }),
    ];
  });
  const pane = await ownerPane(page);
  const turns = pane.getByTestId("structured-turn");
  await expect(turns).toHaveCount(3);
  await expect(turns.nth(0).getByTestId("peer-attribution")).toHaveCount(0); // the operator's own
  await expect(turns.nth(1).getByTestId("peer-attribution")).toHaveText(/^gpu-box\s*·\s*peer$/i);
  await expect(turns.nth(1).getByTestId("peer-edited")).toHaveCount(0);
  await expect(turns.nth(2).getByTestId("peer-attribution")).toHaveText(/^<i>gpu<\/i>-box\s*·\s*peer\s*·\s*edited by you$/i);
  await expect(turns.nth(2).locator("i")).toHaveCount(0); // the name is text
  // it does not read as the operator's turn
  await expect(turns.nth(1).locator('[data-avatar][data-kind="peer"]')).toHaveCount(1);
  await expect(turns.nth(0).locator('[data-avatar][data-kind="peer"]')).toHaveCount(0);
  await noHorizontalOverflow(page);
});

test("the bell entry names the peer and the session, and has no suggested text", async ({ page }) => {
  const SECRET = "SUGGESTED-WORDS-THAT-STAY-ON-THE-CARD";
  const fake = await setup(page, (f) => {
    f.suggestions = [f.suggestion({ text: SECRET })];
    f.notifications = [
      {
        id: "n1",
        ts: NOW() - 5,
        read: false,
        title: "gpu-box suggests a message",
        project: TITLE,
        reason: "A paired instance suggests a message",
        session_id: KEY,
        engine: "codex-api",
        action_id: `peer-suggestion:${f.suggestions[0].suggestion_id}`,
      },
    ];
  });
  await page.goto("/settings/peers"); // somewhere the session's own card is not on screen
  await page.getByRole("button", { name: /^Notifications/ }).first().click();
  // The bell's shared row (`NotificationRow`, #2146) carries the test id; its title is the line.
  const entry = page.getByTestId("bell-peer-suggestion");
  await expect(entry.locator('[class*="title"]')).toHaveText(`gpu-box suggests a message · ${TITLE}`);
  await expect(entry).toContainText("Peer suggestion");
  await expect(entry.locator('[class*="proj"]')).toHaveCount(0); // the title already names the session
  await expect(page.getByText(SECRET)).toHaveCount(0);
  expect(await page.content()).not.toContain(SECRET);
  // it links to the session
  const row = entry;
  await expect(row.getByRole("link", { name: /open/i })).toHaveAttribute("href", `/s/codex-api/${ID}`);
  await row.getByRole("link", { name: /open/i }).click();
  await expect(page).toHaveURL(new RegExp(`/s/codex-api/${ID}$`));
  await expect(cards(page).getByTestId("peer-suggestion-text")).toHaveText(SECRET); // where it belongs
  expect(fake.suggestions).toHaveLength(1);
});

// =================================================================================================
// The receiving side
// =================================================================================================

test("the Suggest composer: suggest, the row waits, withdraw", async ({ page }) => {
  const { fake } = await inputSetup(page, "suggest");
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-banner")).toContainText(/Shared by gpu-box · can suggest · until /);
  await expect(pane.getByTestId("remote-view-only")).toHaveCount(0);
  const composer = pane.getByTestId("remote-composer");
  await expect(composer).toHaveAttribute("data-kind", "suggest");
  await expect(box(pane)).toHaveAttribute("placeholder", "Suggest a message to gpu-box's operator…");
  await expect(go(pane)).toHaveText(/^suggest$/i);
  await expect(go(pane)).toBeDisabled(); // nothing typed

  await say(pane, "Could the queue report how long a job waited?");
  const mine = pane.getByTestId("remote-suggestions");
  await expect(mine.getByRole("heading")).toHaveText(/your suggestions/i);
  const row = mine.getByTestId("remote-suggestion");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("Could the queue report how long a job waited?");
  await expect(row).toContainText("waiting for gpu-box's operator");
  await expect(box(pane)).toHaveValue(""); // accepted: it is no longer the composer's
  expect(writes(fake, SUGGEST_PATH)).toHaveLength(1);
  expect(writes(fake, SUGGEST_PATH)[0]).toMatchObject({
    method: "POST",
    csrf: CSRF,
    body: { text: "Could the queue report how long a job waited?" },
  });
  expect(keys(fake, SUGGEST_PATH)[0]).toMatch(UUID_RE);
  // grey while it waits: it is not THIS operator's attention that is needed
  expect(await row.locator(".hud-led").getAttribute("class")).toContain("idle");
  // it is not in the conversation: a suggestion is not a turn
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);

  await row.getByRole("button", { name: /^Withdraw/ }).click();
  await expect(row).toContainText("withdrawn");
  await expect(row.getByRole("button")).toHaveCount(0);
  const id = fake.own[GRANT_A][0].suggestion_id;
  expect(fake.writes.filter((w) => w.path.endsWith(`/suggestions/${id}/withdraw`))).toHaveLength(1);
  await neverAgain(page, () => writes(fake, SUGGEST_PATH).length, 1, 800);
});

test("each suggestion state in Your suggestions: approving is not approved, expired is not rejected", async ({
  page,
}) => {
  const { fake } = await inputSetup(page, "suggest", (f) => {
    for (const state of ["expired", "withdrawn", "rejected", "approved", "approving", "pending"])
      f.ownRow(GRANT_A, { state, client_key: `key-${state}`, handoff: state === "approved" ? "sent" : null });
  });
  const pane = await openPane(page);
  const rows = pane.getByTestId("remote-suggestion");
  await expect(rows).toHaveCount(6);
  const line = (state: string) => pane.locator(`[data-testid="remote-suggestion"][data-state="${state}"]`);
  await expect(line("pending")).toContainText("waiting for gpu-box's operator");
  await expect(line("pending").getByRole("button", { name: /^Withdraw/ })).toBeVisible();
  await expect(line("approving")).toContainText("approved · being delivered");
  await expect(line("approved")).toContainText(/approved$/);
  await expect(line("approved")).not.toContainText("being delivered");
  await expect(line("rejected")).toContainText("rejected");
  await expect(line("withdrawn")).toContainText("withdrawn");
  await expect(line("expired")).toContainText("expired");
  await expect(line("expired")).not.toContainText("rejected");
  // only a waiting one can be withdrawn; only approved is green
  await expect(rows.getByRole("button")).toHaveCount(1);
  for (const state of ["pending", "approving", "rejected", "withdrawn", "expired"])
    expect(await line(state).locator(".hud-led").getAttribute("class"), state).toContain("idle");
  expect(await line("approved").locator(".hud-led").getAttribute("class")).toContain("up");
  // the owner lists no text, and none is invented for a suggestion this page did not make
  await expect(rows.first()).toContainText(/Suggested at \d\d:\d\d/);
  // the owner decides: the row follows the list
  fake.own[GRANT_A].find((r) => r.state === "pending")!.state = "rejected";
  await expect(line("pending")).toHaveCount(0, { timeout: 10_000 });
  await expect(rows.getByRole("button")).toHaveCount(0);
});

test("the Send composer and each delivery state; Send again reuses the same client_key; not confirmed has no resend", async ({
  page,
}) => {
  const handoffs = ["sent", "queued", "not_sent", "uncertain", null, "sent"];
  const { fake } = await inputSetup(
    page,
    "send",
    (f) => {
      f.onWrite = (kind, _grant, _body, n) =>
        kind === "turns" ? { json: { operation_id_known: true, handoff: handoffs[n - 1] } } : undefined;
    },
    { expires_at: NOW() + 42 * 60 + 30 },
  );
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-banner")).toContainText("Shared by gpu-box · can send · ");
  await expect(pane.getByTestId("remote-countdown")).toHaveText(/^ends in 4[12] min$/);
  await expect(pane.getByTestId("remote-composer")).toHaveAttribute("data-kind", "send");
  await expect(box(pane)).toHaveAttribute("placeholder", "Message gpu-box's agent…");
  await expect(go(pane)).toHaveText(/^send$/i);

  const texts = ["First: sent.", "Second: queued.", "Third: not sent.", "Fourth: uncertain.", "Fifth: no receipt."];
  for (const [i, text] of texts.entries()) {
    await say(pane, text);
    await expect(pane.getByTestId("remote-own-message")).toHaveCount(i + 1);
    await expect(box(pane)).toHaveValue("");
  }
  const own = pane.getByTestId("remote-own-message");
  const delivery = (i: number) => own.nth(i).getByTestId("remote-delivery");
  await expect(own.nth(0)).toContainText("First: sent.");
  await expect(delivery(0)).toHaveText("sent");
  expect(await delivery(0).locator(".hud-led").getAttribute("class")).toContain("up"); // green
  await expect(delivery(1)).toHaveText("queued");
  expect(await delivery(1).locator(".hud-led").getAttribute("class")).toContain("idle");
  await expect(delivery(2)).toContainText("not sent");
  await expect(delivery(2)).toContainText("It did not reach the agent.");
  for (const i of [3, 4]) {
    await expect(delivery(i)).toContainText("delivery not confirmed");
    await expect(delivery(i)).toContainText("It may still arrive. Do not resend blindly.");
    await expect(delivery(i)).toHaveAttribute("data-delivery", "unconfirmed");
    await expect(delivery(i).getByRole("button")).toHaveCount(0); // NO resend control
    await expect(delivery(i)).not.toContainText(/^sent|not sent/);
    expect(await delivery(i).locator(".hud-led").getAttribute("class")).toContain("idle");
  }
  // exactly one resend control on the page: the message the owner says did not arrive
  await expect(pane.getByRole("button", { name: "Send again" })).toHaveCount(1);
  // five messages, five keys
  const sentKeys = keys(fake, SEND_PATH);
  expect(sentKeys).toHaveLength(5);
  expect(new Set(sentKeys).size).toBe(5);
  for (const k of sentKeys) expect(k).toMatch(UUID_RE);

  await delivery(2).getByRole("button", { name: "Send again" }).click();
  await expect(delivery(2)).toHaveText("sent");
  const all = writes(fake, SEND_PATH);
  expect(all).toHaveLength(6);
  expect(all[5].body).toEqual(all[2].body); // the SAME message with the SAME client_key
  expect(all[5].body?.client_key).toBe(sentKeys[2]);
  await expect(pane.getByRole("button", { name: "Send again" })).toHaveCount(0);
  await neverAgain(page, () => writes(fake, SEND_PATH).length, 6);
});

test("the sent turn appears labelled with this instance's name as the owner shows it, its delivery state under it", async ({
  page,
}) => {
  const { fake, convo } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind) => (kind === "turns" ? { json: { operation_id_known: true, handoff: "queued" } } : undefined);
  });
  const pane = await openPane(page);
  await say(pane, "Go with the per-job lock.");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(1);
  // the owner's conversation now shows it, attributed to this instance under the owner's name
  convo.turns = [
    ...RENDER_TURNS,
    turn("aaaaaaaa-0000-4000-8000-0000000000d1", "Go with the per-job lock.", "", {
      reply: null,
      state: "queued",
      author: { name: "studio", via: "peer" },
      mine: true,
    }),
    turn("aaaaaaaa-0000-4000-8000-0000000000d2", "An edited suggestion of ours.", "Done.", {
      author: { name: "studio", via: "peer", edited: true },
      mine: true,
    }),
  ];
  convo.revision = 6;
  const turns = pane.locator("[data-turn]");
  await expect(turns).toHaveCount(4, { timeout: 10_000 });
  await expect(turns.nth(2).getByTestId("peer-attribution")).toHaveText(/^studio\s*·\s*peer$/i);
  await expect(turns.nth(2).getByTestId("remote-delivery")).toHaveText("queued");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0); // drawn once, as the turn
  // to someone who is not the owner an edited turn says "edited", never "by you"
  await expect(turns.nth(3).getByTestId("peer-edited")).toHaveText(/^edited$/i);
  await expect(turns.nth(0).getByTestId("peer-attribution")).toHaveCount(0);
  expect(writes(fake, SEND_PATH)).toHaveLength(1);
});

test("busy: never 'nothing was sent', the text stays, and a retry is the operator's; a new message gets a new key", async ({
  page,
}) => {
  let busy = true;
  const { fake } = await inputSetup(page, "suggest", (f) => {
    f.onWrite = (kind) =>
      kind === "suggest" && busy ? { status: 429, json: { detail: "the other instance is busy — try again", code: "busy" } } : undefined;
  });
  const pane = await openPane(page);
  await say(pane, "Drop the retry limit.");
  // `429 busy` is not proof that nothing was accepted (an owner a build behind says it after
  // the effect too): it is "could not confirm", and the list says what is there (#2061).
  await expect(pane.getByTestId("remote-composer-error")).toHaveText(
    "Could not confirm that the suggestion arrived. Check Your suggestions before suggesting it again.",
  );
  await expect(pane.getByTestId("remote-composer-error")).not.toContainText(/nothing was sent/i);
  await expect(box(pane)).toHaveValue("Drop the retry limit."); // the text stays
  await expect(box(pane)).toBeEnabled();
  await expect(pane.getByTestId("remote-suggestion")).toHaveCount(0);
  // nothing is retried automatically
  await neverAgain(page, () => writes(fake, SUGGEST_PATH).length, 1, 3_500);
  // the operator tries the SAME message again: the same key
  await go(pane).click();
  await expect.poll(() => writes(fake, SUGGEST_PATH).length).toBe(2);
  // other words are another message: a new key, never the old one
  await box(pane).fill("Drop the retry limit, please.");
  await go(pane).click();
  await expect.poll(() => writes(fake, SUGGEST_PATH).length).toBe(3);
  busy = false;
  await go(pane).click(); // the same (edited) message once more: its own key again
  await expect(pane.getByTestId("remote-suggestion")).toHaveCount(1);
  await say(pane, "And a metric for the wait."); // the next message: a new key
  await expect(pane.getByTestId("remote-suggestion")).toHaveCount(2);
  const [k1, k2, k3, k4, k5] = keys(fake, SUGGEST_PATH);
  expect(k2).toBe(k1);
  expect(k3).not.toBe(k1);
  expect(k4).toBe(k3);
  expect(new Set([k1, k3, k5]).size).toBe(3);
  const byKey = new Map<string, Set<string>>();
  for (const w of writes(fake, SUGGEST_PATH)) {
    const k = String(w.body?.client_key);
    byKey.set(k, (byKey.get(k) ?? new Set()).add(String(w.body?.text)));
  }
  for (const [k, texts] of byKey) expect(texts.size, `key ${k} carried one text`).toBe(1);
});

test("held, an expired grant and a lowered level disable the composer and keep the typed text", async ({ page }) => {
  await page.clock.install();
  const { fake } = await inputSetup(
    page,
    "send",
    (f) => {
      f.onWrite = (kind, grant) =>
        kind === "turns" && grant === GRANT_A
          ? { status: 409, json: { detail: "that session is part of a mission right now", code: "held" } }
          : undefined;
    },
    { expires_at: NOW() + 6 * 3600 },
  );
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);

  // held: the owner answers that a mission runs the session
  await say(pane, "Also check the worker count before the next run.");
  await expect(pane.getByTestId("remote-composer-note")).toHaveText(
    "gpu-box's session is run by a mission right now. You can watch.",
  );
  await expect(box(pane)).toBeDisabled();
  await expect(go(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("Also check the worker count before the next run.");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0); // nothing was sent
  await expect(pane.locator("[data-turn]")).toHaveCount(2); // it can still be watched
  expect(writes(fake, SEND_PATH)).toHaveLength(1);

  // a lowered level (the mirror now says view): the other shared session, typed into first
  const list = await showShared(page);
  await list.getByTestId("remote-row").nth(1).locator("a").click();
  await expect(pane.getByTestId("remote-title")).toHaveText("Color pipeline notes");
  await expect(box(pane)).toHaveValue(""); // another session's draft is its own
  await box(pane).fill("Typed while I could still send.");
  fake.remote[1] = { ...fake.remote[1], level: "view", revision: 2 };
  await page.clock.fastForward(31_000); // the list is read again
  await expect(pane.getByTestId("remote-composer-note")).toHaveText(
    "Send access ended. Your text is kept here; ask for access again.",
    { timeout: 10_000 },
  );
  await expect(pane.getByTestId("remote-banner")).toContainText("view only");
  await expect(box(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("Typed while I could still send.");
  await expect(go(pane)).toBeDisabled();

  // an expired grant: back on the first session (its level is still send), past its end
  fake.onWrite = () => undefined;
  await (await showShared(page)).getByTestId("remote-row").first().locator("a").click();
  await expect(pane.getByTestId("remote-title")).toHaveText("Tune the render queue");
  await expect(box(pane)).toBeEnabled();
  await box(pane).fill("Typed just before the grant ran out.");
  await page.clock.fastForward(6 * 3600_000 + 20_000);
  await expect(pane.getByTestId("remote-composer-note")).toHaveText(
    "Send access ended. Your text is kept here; ask for access again.",
    { timeout: 10_000 },
  );
  await expect(box(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("Typed just before the grant ran out.");
  // nothing typed reached storage in any of the three
  const kept = await stored(page);
  for (const text of ["worker count", "Typed while", "Typed just before"]) expect(kept).not.toContain(text);
  expect(writes(fake, SEND_PATH)).toHaveLength(1);
});

test("the owner refuses a write (404): Send access ended, the text stays, nothing is repeated", async ({ page }) => {
  const { fake } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind) =>
      kind === "turns" ? { status: 404, json: { detail: "this session is not shared with this instance", code: "no_grant" } } : undefined;
  });
  const pane = await openPane(page);
  await say(pane, "This will be refused.");
  await expect(pane.getByTestId("remote-composer-note")).toHaveText(
    "Send access ended. Your text is kept here; ask for access again.",
  );
  await expect(box(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("This will be refused.");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  await expect(pane.locator("[data-turn]")).toHaveCount(2); // the reads still work: it is watched
  await neverAgain(page, () => writes(fake, SEND_PATH).length, 1, 2_000);
});

test("peer offline: the composer is disabled and no turns are shown; a final 404 is the neutral state", async ({
  page,
}) => {
  const { convo } = await inputSetup(page, "send");
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await box(pane).fill("Half a thought.");
  convo.mode = "offline";
  await expect(pane.getByTestId("remote-empty")).toHaveText("gpu-box is not connected. Showing nothing until it is back.", {
    timeout: 10_000,
  });
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(box(pane)).toBeDisabled();
  await expect(go(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("Half a thought.");
  await expect(pane.getByTestId("remote-state")).toHaveText("offline");
  convo.mode = "live";
  await expect(pane.locator("[data-turn]")).toHaveCount(2, { timeout: 10_000 });
  await expect(box(pane)).toBeEnabled();
  await expect(box(pane)).toHaveValue("Half a thought.");

  await box(pane).fill("");
  convo.mode = "gone";
  await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.", { timeout: 10_000 });
  await expect(pane.getByTestId("remote-composer")).toHaveCount(0);
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("unavailable");
});

// ---- nothing typed reaches storage --------------------------------------------------------------

test("nothing typed reaches storage: the one record of a request in flight is five fields and no text", async ({
  page,
}) => {
  const held = gate();
  const { fake } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind) =>
      kind === "turns" ? { hold: held.promise, json: { operation_id_known: true, handoff: "sent" } } : undefined;
  });
  const pane = await openPane(page);
  await box(pane).fill("TYPED-BUT-NOT-SENT words");
  expect(await stored(page)).not.toContain("TYPED-BUT-NOT-SENT");
  expect(await records(page)).toEqual([]); // nothing in flight, nothing stored

  await box(pane).fill("IN-FLIGHT-MESSAGE words");
  await go(pane).click();
  await expect.poll(() => writes(fake, SEND_PATH).length).toBe(1);
  const during = await records(page);
  expect(during).toHaveLength(1);
  expect(Object.keys(during[0]).sort()).toEqual(["client_key", "grant_id", "kind", "peer_id", "started_at"]);
  expect(during[0]).toMatchObject({ peer_id: GPU, grant_id: GRANT_A, kind: "send", client_key: keys(fake, SEND_PATH)[0] });
  expect(typeof during[0].started_at).toBe("number");
  const all = await stored(page);
  for (const text of ["IN-FLIGHT-MESSAGE", "TYPED-BUT-NOT-SENT", "render queue", "Tune the render"])
    expect(all, `storage holds "${text}"`).not.toContain(text);

  held.open(); // the answer arrives: the record goes
  await expect(pane.getByTestId("remote-delivery")).toHaveText("sent");
  await expect.poll(() => records(page)).toEqual([]);
  expect(await stored(page)).not.toContain("IN-FLIGHT-MESSAGE");
});

// ---- a reload in the middle of a request --------------------------------------------------------

/** Send (or suggest) `text`, never answer it, and reload: the request is lost in flight. */
async function reloadMidRequest(page: Page, fake: FakeShare, kind: "send" | "suggest", text: string) {
  const path = kind === "send" ? SEND_PATH : SUGGEST_PATH;
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await say(pane, text);
  await expect.poll(() => writes(fake, path).length).toBe(1);
  const key = keys(fake, path)[0];
  expect((await records(page)).map((r) => r.client_key)).toEqual([key]);
  await page.reload();
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  return { pane, key, path };
}
const never = () => new Promise<void>(() => {});

test("a lost response then a reload: the status of the leftover key is read, and a known one shows its real state", async ({
  page,
}) => {
  const { fake } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind, _g, _b, n) => (kind === "turns" && n === 1 ? { hold: never() } : undefined);
    f.onStatus = () => ({ json: { known: true, kind: "send", handoff: "queued" } });
  });
  const { pane, key } = await reloadMidRequest(page, fake, "send", "LOST-IN-FLIGHT one");
  // the owner knows the key: its real state, no notice, and the record is gone
  const own = pane.getByTestId("remote-own-message");
  await expect(own).toHaveCount(1);
  await expect(own.getByTestId("remote-delivery")).toHaveText("queued");
  await expect(own).toContainText("A message you sent before the reload.");
  await expect(pane.getByTestId("remote-reload-notice")).toHaveCount(0);
  expect(fake.statusReads).toEqual([key]);
  await expect.poll(() => records(page)).toEqual([]);
  // nothing was resubmitted, the text is nowhere, and the composer is empty
  await expect(box(pane)).toHaveValue("");
  await expect(page.getByText("LOST-IN-FLIGHT")).toHaveCount(0);
  expect(await stored(page)).not.toContain("LOST-IN-FLIGHT");
  await neverAgain(page, () => writes(fake, SEND_PATH).length, 1, 2_000);
  expect(fake.statusReads).toEqual([key]); // asked once: it is accounted for
  // the next message uses a new key
  await say(pane, "The next message.");
  await expect.poll(() => writes(fake, SEND_PATH).length).toBe(2);
  expect(keys(fake, SEND_PATH)[1]).not.toBe(key);
});

test("a lost suggestion then a reload: a known one is in Your suggestions with its state", async ({ page }) => {
  const { fake } = await inputSetup(page, "suggest", (f) => {
    f.onWrite = (kind, _g, body, n) => {
      if (kind !== "suggest" || n !== 1) return undefined;
      // the owner recorded it; the answer never reached this browser
      f.ownRow(GRANT_A, { client_key: String(body?.client_key), state: "pending" });
      return { hold: never() };
    };
    f.onStatus = () => ({
      json: {
        known: true,
        kind: "suggest",
        suggestion_id: f.own[GRANT_A][0].suggestion_id,
        state: "pending",
        handoff: null,
      },
    });
  });
  const { pane, key } = await reloadMidRequest(page, fake, "suggest", "LOST-SUGGESTION words");
  const row = pane.getByTestId("remote-suggestion");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("waiting for gpu-box's operator");
  await expect(pane.getByTestId("remote-reload-notice")).toHaveCount(0);
  await expect.poll(() => records(page)).toEqual([]);
  expect(fake.statusReads[0]).toBe(key);
  await expect(page.getByText("LOST-SUGGESTION")).toHaveCount(0); // its text was never stored
  await neverAgain(page, () => writes(fake, SUGGEST_PATH).length, 1, 2_000);
});

test("reload, status unknown: 'may or may not have arrived', never non-arrival; nothing is resubmitted; given up after two minutes with the same words", async ({
  page,
}) => {
  await page.clock.install();
  const { fake } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind, _g, _b, n) => (kind === "turns" && n === 1 ? { hold: never() } : undefined);
  });
  const { pane, key } = await reloadMidRequest(page, fake, "send", "MAYBE-ARRIVED words");
  const notice = pane.getByTestId("remote-reload-notice");
  await expect(notice).toHaveText(UNKNOWN_NOTICE);
  // unknown is not "did not arrive": no such claim anywhere, no row, no resend control
  await expect(pane).not.toContainText(/not sent|did not (arrive|reach)|failed/i);
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  await expect(pane.getByRole("button", { name: /send again|resend|retry/i })).toHaveCount(0);
  expect((await records(page)).map((r) => r.client_key)).toEqual([key]); // kept: it is asked again
  expect(await stored(page)).not.toContain("MAYBE-ARRIVED");
  await expect(box(pane)).toHaveValue("");
  expect(writes(fake, SEND_PATH)).toHaveLength(1);

  // asked again at 5 s and at 30 s
  await page.clock.fastForward(5_500);
  await expect.poll(() => fake.statusReads.length).toBe(2);
  await page.clock.fastForward(25_500);
  await expect.poll(() => fake.statusReads.length).toBe(3);
  await expect(notice).toHaveText(UNKNOWN_NOTICE);
  // the next message the operator writes gets a new key, and is sent once
  await say(pane, "A message written after the reload.");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(1);
  expect(keys(fake, SEND_PATH)).toHaveLength(2);
  expect(keys(fake, SEND_PATH)[1]).not.toBe(key);

  // two minutes of unknown: the notice stays as written, the record goes, nothing more is asked
  await page.clock.fastForward(95_000);
  await expect.poll(() => records(page)).toEqual([]);
  await expect(notice).toHaveText(UNKNOWN_NOTICE);
  const asked = fake.statusReads.length;
  await page.clock.fastForward(120_000);
  await painted(page);
  expect(fake.statusReads.length).toBe(asked);
  expect(fake.statusReads.every((k) => k === key)).toBe(true);
  // in all of this the original was never submitted again
  expect(writes(fake, SEND_PATH).filter((w) => w.body?.client_key === key)).toHaveLength(1);
  expect(writes(fake, SEND_PATH)).toHaveLength(2);
});

test("a delayed original request: unknown after the reload, admitted later, and the next check shows the real state", async ({
  page,
}) => {
  let admitted = false;
  const { fake } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind, _g, _b, n) => (kind === "turns" && n === 1 ? { hold: never() } : undefined);
    f.onStatus = () => ({ json: admitted ? { known: true, kind: "send", handoff: "sent" } : { known: false } });
  });
  const { pane, key } = await reloadMidRequest(page, fake, "send", "DELAYED-ORIGINAL words");
  const notice = pane.getByTestId("remote-reload-notice");
  await expect(notice).toHaveText(UNKNOWN_NOTICE);
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  expect(fake.statusReads).toEqual([key]);
  // the request from before the reload was still travelling: the owner admits it only now
  admitted = true;
  // ...and the pane's next check (5 s after the first) replaces the notice with the real state
  await expect(pane.getByTestId("remote-own-message").getByTestId("remote-delivery")).toHaveText("sent", {
    timeout: 12_000,
  });
  await expect(notice).toHaveCount(0);
  expect(fake.statusReads.length).toBeGreaterThanOrEqual(2);
  await expect.poll(() => records(page)).toEqual([]);
  expect(writes(fake, SEND_PATH)).toHaveLength(1); // found out, never resubmitted
  expect(await stored(page)).not.toContain("DELAYED-ORIGINAL");
});

test("an unknown key is asked about again when the conversation changes", async ({ page }) => {
  await page.clock.install();
  let admitted = false;
  const { fake, convo } = await inputSetup(page, "send", (f) => {
    f.onWrite = (kind, _g, _b, n) => (kind === "turns" && n === 1 ? { hold: never() } : undefined);
    f.onStatus = () => ({ json: admitted ? { known: true, kind: "send", handoff: "sent" } : { known: false } });
  });
  const { pane } = await reloadMidRequest(page, fake, "send", "ADMITTED-WITH-THE-TURN words");
  await expect(pane.getByTestId("remote-reload-notice")).toHaveText(UNKNOWN_NOTICE);
  // past the 5 s and 30 s checks: nothing is scheduled any more until the two minutes are up
  await page.clock.fastForward(5_500);
  await expect.poll(() => fake.statusReads.length).toBe(2);
  await page.clock.fastForward(25_500);
  await expect.poll(() => fake.statusReads.length).toBe(3);
  await page.clock.fastForward(10_000);
  await painted(page);
  expect(fake.statusReads.length).toBe(3);
  // the conversation moves (the owner admitted the message: its turn is there)
  admitted = true;
  convo.turns = [
    ...RENDER_TURNS,
    turn("aaaaaaaa-0000-4000-8000-0000000000e1", "ADMITTED-WITH-THE-TURN words", "On it.", {
      author: { name: "studio", via: "peer" },
      mine: true,
    }),
  ];
  convo.revision = 6;
  await page.clock.fastForward(1_500);
  await expect(pane.locator("[data-turn]")).toHaveCount(3, { timeout: 10_000 });
  await expect.poll(() => fake.statusReads.length).toBe(4);
  await expect(pane.getByTestId("remote-reload-notice")).toHaveCount(0);
  await expect(pane.getByTestId("remote-own-message").getByTestId("remote-delivery")).toHaveText("sent");
  expect(writes(fake, SEND_PATH)).toHaveLength(1);
});

for (const [name, failure] of [
  ["the peer is offline (503)", { status: 503, json: { detail: "not connected", code: "peer_offline" } }],
  ["the owner refuses (404)", { status: 404, json: { detail: "not shared", code: "no_grant" } }],
  ["an error (502)", { status: 502, json: { detail: "bad answer", code: "bad_answer" } }],
] as [string, Answer][]) {
  test(`a failed status read (${name}) shows 'Could not check' and keeps the record`, async ({ page }) => {
    let failing = true;
    const { fake } = await inputSetup(page, "send", (f) => {
      f.onWrite = (kind, _g, _b, n) => (kind === "turns" && n === 1 ? { hold: never() } : undefined);
      f.onStatus = () => (failing ? failure : { json: { known: true, kind: "send", handoff: "not_sent" } });
    });
    const { pane, key } = await reloadMidRequest(page, fake, "send", "UNCHECKED words");
    const notice = pane.getByTestId("remote-reload-notice");
    await expect(notice).toHaveText(UNCHECKED_NOTICE);
    // never reported as absence, and never as the "may or may not" of an owner that answered
    await expect(pane).not.toContainText(/not sent|did not (arrive|reach)|may or may not/i);
    await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
    expect((await records(page)).map((r) => r.client_key)).toEqual([key]); // the record is kept
    expect(writes(fake, SEND_PATH)).toHaveLength(1);
    // retried while the peer can be reached
    await expect.poll(() => fake.statusReads.length, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
    await expect(notice).toHaveText(UNCHECKED_NOTICE);
    expect((await records(page)).map((r) => r.client_key)).toEqual([key]);
    // reachable again: the real state. A message that did not arrive is said so only NOW, on the
    // owner's word; its text was never stored, so there is nothing to send again.
    failing = false;
    const own = pane.getByTestId("remote-own-message");
    await expect(own.getByTestId("remote-delivery")).toContainText("not sent", { timeout: 15_000 });
    await expect(own.getByRole("button")).toHaveCount(0);
    await expect(notice).toHaveCount(0);
    await expect.poll(() => records(page)).toEqual([]);
    expect(writes(fake, SEND_PATH)).toHaveLength(1);
    expect(await stored(page)).not.toContain("UNCHECKED words");
  });
}

// ---- late answers are fenced --------------------------------------------------------------------

async function heldSend(page: Page, kind: "send" | "suggest" = "send") {
  const held = gate();
  const setupResult = await inputSetup(page, kind, (f) => {
    f.onWrite = (k, grant, _b, n) => {
      if (grant !== GRANT_A || n !== 1) return undefined;
      if (k === "turns")
        return { hold: held.promise, label: "late", json: { operation_id_known: true, handoff: "sent" } };
      if (k === "suggest")
        return { hold: held.promise, label: "late", json: { suggestion_id: "late".padEnd(22, "x"), state: "pending" } };
      return undefined;
    };
  });
  await page.goto(URL_PATH);
  const list = await showShared(page);
  await list.getByTestId("remote-row").first().locator("a").click();
  const pane = page.getByTestId("remote-pane");
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await say(pane, "LATE-MESSAGE words");
  const path = kind === "send" ? SEND_PATH : SUGGEST_PATH;
  await expect.poll(() => writes(setupResult.fake, path).length).toBe(1);
  return { ...setupResult, pane, held, path };
}
/** The held answer is delivered to the page, and whatever it would paint is painted. */
async function release(page: Page, fake: FakeShare, held: { open: () => void }) {
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  await page.waitForTimeout(300);
  await painted(page);
}

for (const kind of ["send", "suggest"] as const) {
  test(`a late ${kind} success after access was lost does not refill the pane or re-enable the composer`, async ({
    page,
  }) => {
    const { fake, convo, pane, held, path } = await heldSend(page, kind);
    convo.mode = "gone"; // the share ended while the answer was on its way
    await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.", { timeout: 10_000 });
    await expect(box(pane)).toBeDisabled(); // the text typed is kept, in a composer that is off
    await expect(box(pane)).toHaveValue("LATE-MESSAGE words");
    await release(page, fake, held);
    await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
    await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
    await expect(pane.getByTestId("remote-delivery")).toHaveCount(0);
    await expect(pane.getByTestId("remote-suggestion")).toHaveCount(0);
    await expect(pane.locator("[data-turn]")).toHaveCount(0);
    await expect(box(pane)).toBeDisabled();
    await expect(go(pane)).toBeDisabled();
    await expect(box(pane)).toHaveValue("LATE-MESSAGE words"); // not cleared by the late answer
    await expect(pane.getByTestId("remote-state")).toHaveText("unavailable");
    expect(writes(fake, path)).toHaveLength(1);
  });
}

test("a late success after the peer went offline does not refill the pane, re-enable the composer or touch the draft", async ({
  page,
}) => {
  const { fake, convo, pane, held } = await heldSend(page);
  convo.mode = "offline";
  await expect(pane.getByTestId("remote-empty")).toContainText("gpu-box is not connected", { timeout: 10_000 });
  await expect(box(pane)).toBeDisabled();
  await release(page, fake, held);
  // still offline, still nothing shown, the composer still off and its text untouched
  await expect(pane.getByTestId("remote-empty")).toContainText("gpu-box is not connected");
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  await expect(box(pane)).toBeDisabled();
  await expect(box(pane)).toHaveValue("LATE-MESSAGE words");
  await expect(pane.getByTestId("remote-state")).toHaveText("offline");
  // back online: the pane fills from the owner's own answer, and nothing of the late one is there
  convo.mode = "live";
  await expect(pane.locator("[data-turn]")).toHaveCount(2, { timeout: 10_000 });
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  await expect(pane.getByTestId("remote-delivery")).toHaveCount(0);
  await expect(box(pane)).toBeEnabled();
  await expect(box(pane)).toHaveValue("LATE-MESSAGE words");
  expect(writes(fake, SEND_PATH)).toHaveLength(1); // and it was not sent again
});

test("a late success after switching sessions lands in neither pane and leaves the new draft alone", async ({ page }) => {
  const { fake, pane, held } = await heldSend(page);
  const list = await showShared(page);
  await list.getByTestId("remote-row").nth(1).locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_B}$`));
  await expect(pane.getByTestId("remote-title")).toHaveText("Color pipeline notes");
  await expect(pane.locator("[data-turn]")).toHaveCount(1);
  // the new pane's composer is its own: empty, enabled (the other session's send does not hold it)
  await expect(box(pane)).toHaveValue("");
  await expect(box(pane)).toBeEnabled();
  await box(pane).fill("A NEW DRAFT for the other session");
  await expect(go(pane)).toBeEnabled();
  await release(page, fake, held);
  await expect(box(pane)).toHaveValue("A NEW DRAFT for the other session"); // not overwritten, not cleared
  await expect(go(pane)).toBeEnabled();
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(0);
  await expect(pane.getByTestId("remote-delivery")).toHaveCount(0);
  await expect(page.getByText("LATE-MESSAGE")).toHaveCount(0);
  await expect(pane.locator("[data-turn]")).toHaveCount(1);
  await expect(pane.getByTestId("remote-title")).toHaveText("Color pipeline notes");
  expect(fake.writes.filter((w) => w.path.endsWith("/turns"))).toHaveLength(1);
});

// ---- the phone ----------------------------------------------------------------------------------

test("controls are at least 44 px on the phone: the cards, the edit mode, the composers, Your suggestions, Send again", async ({
  page,
}) => {
  test.skip(!isMobile(page), "the phone layout");
  await page.setViewportSize({ width: 390, height: 760 });
  const handoffs = ["not_sent", "uncertain"];
  const { fake } = await inputSetup(page, "send", (f) => {
    f.suggestions = [f.suggestion({ text: "x".repeat(400) }), f.suggestion({ text: "Second." })];
    f.ownRow(GRANT_A, { state: "pending", client_key: "k1" });
    f.ownRow(GRANT_A, { state: "approved", client_key: "k2" });
    f.onWrite = (kind, _g, _b, n) =>
      kind === "turns" ? { json: { operation_id_known: true, handoff: handoffs[n - 1] } } : undefined;
  });
  // the owner's cards, and one in edit mode
  await ownerPane(page);
  const zone = page.getByTestId("peer-suggestions");
  await expect(cards(page)).toHaveCount(2);
  await controlsAtLeast44(zone);
  await noHorizontalOverflow(page);
  await cards(page).first().getByRole("button", { name: "Edit", exact: true }).click();
  await controlsAtLeast44(zone);
  const edit = (await cards(page).first().getByTestId("peer-suggestion-edit").boundingBox())!;
  expect(edit.x + edit.width).toBeLessThanOrEqual(390);
  await noHorizontalOverflow(page);

  // the receiving pane: Your suggestions, the delivery rows, the composer
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await say(pane, "Not sent.");
  await say(pane, "Not confirmed.");
  await expect(pane.getByTestId("remote-own-message")).toHaveCount(2);
  await expect(pane.getByRole("button", { name: "Send again" })).toHaveCount(1);
  await expect(pane.getByTestId("remote-suggestion")).toHaveCount(2);
  await controlsAtLeast44(pane);
  const text = (await box(pane).boundingBox())!;
  expect(text.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await noHorizontalOverflow(page);
  const wide = await pane.evaluate((p) =>
    [...p.querySelectorAll<HTMLElement>("form, form *, [data-testid=remote-suggestions] *, [data-testid=remote-delivery]")]
      .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
      .map((el) => el.className),
  );
  expect(wide).toEqual([]);
  expect(writes(fake, SEND_PATH)).toHaveLength(2);
});

// =================================================================================================
// A terminal is shared View only (#2060): the levels, the risk and the composer are an API's
// =================================================================================================

test("a terminal's dialog offers View alone, no risk box, and renews even a terminal grant listed above View as View", async ({
  page,
}) => {
  // A terminal grant the owner's list shows at `send` (written behind the rules, or by a build
  // that had a bug): the dialog reads no level into its choice, and asks for View.
  const fake = await setupTerminal(page, (f) => {
    f.grants.push(f.grant(GPU, { session_key: TERM_KEY, kind: "terminal", level: "send", title: TERM_TITLE }));
  });
  await page.goto(TERM_URL);
  await expect(page.locator(".xterm-rows")).toBeVisible();
  await clickHeadAction(page, "Share this terminal's screen with a paired instance");
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toHaveAttribute("data-kind", "terminal");
  await expect(dialog.getByTestId("share-peer-existing")).toBeVisible();
  const access = dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio");
  await expect(access).toHaveCount(1);
  await expect(access.first()).toHaveAttribute("data-level", "view");
  await expect(dialog.getByTestId("share-peer-risk")).toHaveCount(0);
  await expect(dialog.getByRole("checkbox")).toHaveCount(0);
  await expect(dialog.getByTestId("share-peer-suggest-line")).toHaveCount(0);
  await expect(shareButton(dialog)).toBeEnabled();
  await shareButton(dialog).click();
  await expect(dialog.getByTestId("share-peer-done")).toContainText("Shared with gpu-box (View)");
  const [patch] = grantWrites(fake);
  expect(patch.method).toBe("PATCH");
  expect(patch.body).toEqual({ level: "view", expires_in: 7 * DAY, revision: 1 });
  expect(fake.grants[0].level).toBe("view");
  await noHorizontalOverflow(page);
});

for (const level_ of ["suggest", "send"] as const) {
  test(`a terminal share listed at ${level_} opens the screen pane: no composer, nothing to write with`, async ({
    page,
  }) => {
    const fake = await setup(page, (f) => {
      f.remote.push({ ...f.screenRow(GPU, "gpu-box", GRANT_T, TERM_TITLE, true), level: level_ });
    });
    await page.goto(`/remote/${GPU}/${GRANT_T}`);
    const pane = page.getByTestId("remote-pane");
    await expect(pane).toBeVisible();
    await expect(pane).toHaveAttribute("aria-label", /screen only/);
    await expect.poll(() => fake.reads.filter((r) => r.kind === "frame" && r.grant === GRANT_T).length).toBeGreaterThan(0);
    await expect(pane.getByTestId("remote-composer")).toHaveCount(0);
    await expect(page.getByRole("textbox")).toHaveCount(0);
    await expect(pane.getByTestId("remote-suggestions")).toHaveCount(0);
    expect(fake.reads.filter((r) => r.grant === GRANT_T && r.kind !== "frame")).toEqual([]);
    expect(fake.writes.filter((w) => w.path.includes(GRANT_T))).toEqual([]);
    await noHorizontalOverflow(page);
  });
}

// =================================================================================================
// A peer's numbers
// =================================================================================================

test("Your suggestions with times no Date can hold: every row is drawn, as an unknown time", async ({ page }) => {
  // JSON carries these (NaN and Infinity cannot travel; the unit tests plant those).
  const hostile: unknown[] = [1e300, -1, 0, "14:05", null, 8.64e12 + 1];
  await inputSetup(page, "suggest", (f) => {
    hostile.forEach((created_at, i) => f.ownRow(GRANT_A, { state: "pending", client_key: `bad-${i}`, created_at }));
    f.ownRow(GRANT_A, { state: "approved", client_key: "good" });
  });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const pane = await openPane(page);
  const rows = pane.getByTestId("remote-suggestion");
  await expect(rows).toHaveCount(hostile.length + 1);
  await expect(rows.filter({ hasText: "Suggested at an unknown time" })).toHaveCount(hostile.length);
  await expect(rows.filter({ hasText: /Suggested at \d\d:\d\d/ })).toHaveCount(1);
  await expect(pane.locator("[data-turn]")).toHaveCount(2); // the pane is still the pane
  await expect(box(pane)).toBeEnabled();
  expect(errors).toEqual([]);
});
