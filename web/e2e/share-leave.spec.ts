import { expect, test, type Locator, type Page } from "@playwright/test";
import { TOUCH_FLOOR } from "./touchTarget";
import { settingsPath } from "../src/routes/settingsTabs";
import { SESSIONS_PATH } from "../src/lib/routes";
import {
  CSRF,
  GPU,
  GRANT_A,
  GRANT_B,
  GRANT_T,
  LAPTOP,
  TITLE,
  type FakeShare,
  controlsAtLeast44,
  gate,
  isMobile,
  noHorizontalOverflow,
  openPane,
  painted,
  setup,
  showShared,
} from "./shareFixture";

// Leaving a share (#2201): the receiving instance gives up ONE session a peer shares with it, in a
// real browser on both projects, against the in-test model of `routes/federation.py`
// (`shareFixture.ts`, extended with the leave route).
//
// What a browser alone can show: that the button is a real control in the session's banner and in
// Settings → Peers (44 px on a phone), that the dialog opens with focus on Cancel and that Cancel
// changes nothing, that a proven leave puts the app on the session list with the row gone from
// the Shared tab, and that the three other outcomes keep the row: offline, refused (Leave is
// offered again and sends the revision listed then), and not known (Check again READS the list
// and never sends Leave a second time).

const API_TITLE = "Tune the render queue";
const SCREEN_TITLE = "Bench the new tokenizer";

const dialogOf = (page: Page) => page.getByTestId("leave-dialog");
const closeButton = (dialog: Locator) => dialog.locator("button", { hasText: /^Close$/ });
const onSessionList = (page: Page) => expect(page).toHaveURL(new RegExp(`${SESSIONS_PATH}$`));
const onShare = (page: Page, grant: string) => expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${grant}$`));

async function sharedRows(page: Page): Promise<Locator> {
  const list = await showShared(page);
  return list.getByTestId("remote-row");
}

/** The Leave button of an open pane: a plain button in the banner, never a menu item. */
async function leaveIn(page: Page, pane: Locator): Promise<Locator> {
  const button = pane.getByTestId("share-leave");
  await expect(button).toBeVisible();
  await expect(pane.getByTestId("remote-banner").getByTestId("share-leave")).toHaveCount(1);
  await expect(button).toHaveText("Leave");
  expect(await button.evaluate((el) => el.tagName === "BUTTON" && !el.closest('[role="menu"]'))).toBe(true);
  if (isMobile(page)) {
    const box = (await button.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.width).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  return button;
}

/** Press Leave and wait for the question: it names the session and the peer, focus is on Cancel. */
async function ask(page: Page, button: Locator, title: string): Promise<Locator> {
  await button.click();
  const dialog = dialogOf(page);
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("data-phase", "confirm");
  await expect(dialog.getByRole("heading", { name: "Leave this share" })).toBeVisible();
  await expect(dialog.getByTestId("leave-lead")).toHaveText(`Stop receiving ${title} from gpu-box?`);
  await expect(dialog.getByTestId("leave-sub")).toContainText("The share ends on both instances now.");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  if (isMobile(page)) await controlsAtLeast44(dialog);
  return dialog;
}

test("an API share's header: Leave asks first, Cancel changes nothing, Leave ends it and shows the session list", async ({
  page,
}) => {
  const fake = await setup(page);
  const pane = await openPane(page, GRANT_A);
  await expect(pane.getByTestId("remote-title")).toHaveText(API_TITLE);
  const button = await leaveIn(page, pane);
  await noHorizontalOverflow(page);

  // Cancel: everything is as it was, and the button has the focus back.
  let dialog = await ask(page, button, API_TITLE);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(button).toBeFocused();
  await onShare(page, GRANT_A);
  expect(fake.leaves).toEqual([]);
  // Escape is Cancel too.
  dialog = await ask(page, button, API_TITLE);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(fake.leaves).toEqual([]);
  expect(fake.remote.map((r) => r.grant_id)).toContain(GRANT_A);

  // Leave: one request with the listed revision; the app is on the session list.
  dialog = await ask(page, button, API_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await onSessionList(page);
  await expect(dialog).toHaveCount(0);
  expect(fake.leaves).toEqual([{ peer: GPU, grant: GRANT_A, revision: 1 }]);
  expect(fake.writes.filter((w) => w.path.endsWith("/leave"))).toEqual([
    { method: "POST", path: `/remote/${GPU}/${GRANT_A}/leave`, body: { revision: 1 }, csrf: CSRF },
  ]);
  // The row is gone from the Shared tab; the other shares are untouched.
  const rows = await sharedRows(page);
  await expect(rows).toHaveCount(2);
  await expect(rows.filter({ hasText: API_TITLE })).toHaveCount(0);
  await expect(rows.nth(0)).toContainText("Color pipeline notes");
});

test("a terminal share's header has the same Leave, and it ends the same way", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.remote = [...f.remote, f.screenRow(GPU, "gpu-box", GRANT_T, SCREEN_TITLE, true)];
  });
  const pane = await openPane(page, GRANT_T);
  await expect(pane.getByTestId("remote-screen-bar")).toBeVisible();
  const button = await leaveIn(page, pane);
  const dialog = await ask(page, button, SCREEN_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await onSessionList(page);
  expect(fake.leaves).toEqual([{ peer: GPU, grant: GRANT_T, revision: 1 }]);
  const rows = await sharedRows(page);
  await expect(rows).toHaveCount(3);
  await expect(rows.filter({ hasText: SCREEN_TITLE })).toHaveCount(0);
});

test("Settings → Peers: Leave beside each session a peer shares; Revoke stays on what you share", async ({
  page,
}) => {
  const fake = await setup(page, (f) => {
    f.grants = [f.grant(GPU, { grant_id: "grantOut".padEnd(22, "x"), title: TITLE })];
  });
  await page.goto(settingsPath("peers"));
  const gpu = page.locator(`[data-testid="peer-row"][data-peer="${GPU}"]`);
  await gpu.getByRole("button", { name: /Sessions · 1 shared · 2 received/ }).click();
  const inbound = gpu.getByTestId("peer-shares-in");
  const received = inbound.getByTestId("peer-received");
  await expect(received).toHaveCount(2);
  // Each received share has Leave and nothing else; each share of ours keeps Revoke and no Leave.
  await expect(inbound.getByRole("button")).toHaveCount(2);
  await expect(received.nth(0).getByTestId("share-leave")).toHaveAccessibleName(
    `Leave ${API_TITLE}, shared by gpu-box`,
  );
  const out = gpu.getByTestId("peer-shares-out");
  await expect(out.getByRole("button", { name: `Revoke ${TITLE} for gpu-box` })).toBeVisible();
  await expect(out.getByTestId("share-leave")).toHaveCount(0);
  if (isMobile(page)) await controlsAtLeast44(inbound);
  await noHorizontalOverflow(page);

  // Cancel changes nothing.
  const button = received.nth(0).getByTestId("share-leave");
  let dialog = await ask(page, button, API_TITLE);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(button).toBeFocused();
  await expect(received).toHaveCount(2);
  expect(fake.leaves).toEqual([]);

  // Leave: that row goes, the count follows, the page stays, and the other lists are untouched.
  dialog = await ask(page, button, API_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveCount(0);
  await expect(received).toHaveCount(1);
  await expect(received).toContainText("Color pipeline notes");
  await expect(inbound.getByRole("heading", { name: "Shared by gpu-box · 1" })).toBeVisible();
  await expect(gpu.getByTestId("peer-shares-counts")).toHaveText("Sessions · 1 shared · 1 received");
  await expect(page).toHaveURL(new RegExp(`${settingsPath("peers")}$`));
  expect(fake.leaves).toEqual([{ peer: GPU, grant: GRANT_A, revision: 1 }]);
  await expect(out.getByTestId("peer-grant")).toHaveCount(1);
  await expect(
    page.locator(`[data-testid="peer-row"][data-peer="${LAPTOP}"]`).getByTestId("peer-shares-counts"),
  ).toHaveText("Sessions · 0 shared · 1 received");
  // Focus is somewhere on the page again, not lost behind the closed dialog.
  await expect.poll(() => page.evaluate(() => document.activeElement !== document.body)).toBe(true);
});

test("the owner is not connected: the dialog says so, nothing changed, and the row stays", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.onLeave = () => ({
      status: 503,
      json: { detail: "the instance that shares this session is not connected", code: "peer_offline" },
    });
  });
  const pane = await openPane(page, GRANT_A);
  const dialog = await ask(page, await leaveIn(page, pane), API_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveAttribute("data-phase", "offline");
  await expect(dialog.getByTestId("leave-lead")).toHaveText("gpu-box is not connected.");
  await expect(dialog.getByTestId("leave-sub")).toContainText("Nothing changed.");
  await expect(dialog.getByTestId("leave-sub")).toContainText("revoke the peer in Settings → Peers");
  await expect(dialog.getByTestId("leave-confirm")).toBeDisabled();
  if (isMobile(page)) await controlsAtLeast44(dialog);
  await closeButton(dialog).click();
  await expect(dialog).toHaveCount(0);
  await onShare(page, GRANT_A);
  expect(fake.leaves).toHaveLength(1);
  const rows = await sharedRows(page);
  await expect(rows).toHaveCount(3);
  await expect(rows.filter({ hasText: API_TITLE })).toHaveCount(1);
});

test("the answer was lost: the row stays, and Check again reads the list and sends nothing", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.onLeave = () => ({
      status: 504,
      json: { detail: "the connection to the other instance ended before it answered", code: "peer_lost" },
    });
  });
  const pane = await openPane(page, GRANT_A);
  const dialog = await ask(page, await leaveIn(page, pane), API_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveAttribute("data-phase", "unconfirmed");
  await expect(dialog.getByTestId("leave-lead")).toHaveText("We could not confirm that you left.");
  await expect(dialog.getByTestId("leave-sub")).toContainText("may or may not have ended");
  // Neither a refusal nor success: no Leave to press, the session is still on screen.
  await expect(dialog).not.toContainText("did not accept");
  await expect(dialog.getByTestId("leave-confirm")).toHaveCount(0);
  await onShare(page, GRANT_A);
  if (isMobile(page)) await controlsAtLeast44(dialog);

  // Check again: exactly one read of the list, and no Leave. The owner still lists the share.
  const check = dialog.getByTestId("leave-check");
  await expect(check).toHaveText("Check again");
  await painted(page);
  let reads = fake.listReads;
  await check.click();
  await expect(dialog.getByTestId("leave-note")).toHaveText("Checked: it is still in your list.");
  expect(fake.listReads).toBe(reads + 1);
  expect(fake.leaves).toHaveLength(1);
  await onShare(page, GRANT_A);

  // The owner's list no longer has it (it did end): the next check closes and shows the list.
  fake.remote = fake.remote.filter((r) => r.grant_id !== GRANT_A);
  reads = fake.listReads;
  await check.click();
  await onSessionList(page);
  await expect(dialog).toHaveCount(0);
  expect(fake.listReads).toBeGreaterThanOrEqual(reads + 1);
  expect(fake.leaves).toHaveLength(1); // one Leave, in all of this
  const rows = await sharedRows(page);
  await expect(rows.filter({ hasText: API_TITLE })).toHaveCount(0);
});

test("an uncertain answer keeps the row in the Shared tab", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.onLeave = () => ({ status: 429, json: { detail: "the other instance is busy — try again", code: "busy" } });
  });
  const pane = await openPane(page, GRANT_A);
  const dialog = await ask(page, await leaveIn(page, pane), API_TITLE);
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveAttribute("data-phase", "unconfirmed");
  await closeButton(dialog).click();
  await onShare(page, GRANT_A);
  const rows = await sharedRows(page);
  await expect(rows).toHaveCount(3);
  await expect(rows.filter({ hasText: API_TITLE })).toHaveCount(1);
  expect(fake.leaves).toHaveLength(1);
});

test("refused: the share is still there, and Leave again sends the revision the owner lists now", async ({
  page,
}) => {
  const fake = await setup(page);
  // The owner changed the share just before Leave arrived: the revision this page holds is stale.
  fake.onLeave = (grant, _body, n) => {
    if (n > 1) return undefined;
    const row = fake.remote.find((r) => r.grant_id === grant)!;
    Object.assign(row, { revision: 2, level: "suggest" });
    return { status: 404, json: { detail: "this session is not shared with this instance", code: "no_grant" } };
  };
  const pane = await openPane(page, GRANT_A);
  const dialog = await ask(page, await leaveIn(page, pane), API_TITLE);
  const reads = fake.listReads;
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveAttribute("data-phase", "refused");
  await expect(dialog.getByTestId("leave-lead")).toHaveText("gpu-box did not accept that.");
  await expect(dialog.getByTestId("leave-sub")).toContainText("it is still in your list");
  expect(fake.listReads).toBeGreaterThan(reads); // the list was read again
  expect(fake.leaves).toEqual([{ peer: GPU, grant: GRANT_A, revision: 1 }]); // the write was not
  await onShare(page, GRANT_A);
  // The row stays, as the owner has it now.
  await expect(pane.getByTestId("remote-banner")).toContainText("can suggest");
  if (isMobile(page)) await controlsAtLeast44(dialog);

  // Leave is available again; the operator's press sends the revision listed now, and it ends.
  await expect(dialog.getByTestId("leave-confirm")).toBeEnabled();
  await dialog.getByTestId("leave-confirm").click();
  await onSessionList(page);
  expect(fake.leaves).toEqual([
    { peer: GPU, grant: GRANT_A, revision: 1 },
    { peer: GPU, grant: GRANT_A, revision: 2 },
  ]);
  const rows = await sharedRows(page);
  await expect(rows.filter({ hasText: API_TITLE })).toHaveCount(0);
});

test("refused and kept: closing the dialog leaves the row in the Shared tab", async ({ page }) => {
  const fake: FakeShare = await setup(page, (f) => {
    f.onLeave = () => ({
      status: 404,
      json: { detail: "this session is not shared with this instance", code: "no_grant" },
    });
  });
  await page.goto(settingsPath("peers"));
  const gpu = page.locator(`[data-testid="peer-row"][data-peer="${GPU}"]`);
  await gpu.getByRole("button", { name: /Sessions · 0 shared · 2 received/ }).click();
  const received = gpu.getByTestId("peer-received");
  const dialog = await ask(page, received.nth(1).getByTestId("share-leave"), "Color pipeline notes");
  await dialog.getByTestId("leave-confirm").click();
  await expect(dialog).toHaveAttribute("data-phase", "refused");
  await expect(dialog.getByTestId("leave-confirm")).toBeEnabled();
  await closeButton(dialog).click();
  await expect(received).toHaveCount(2);
  expect(fake.leaves).toEqual([{ peer: GPU, grant: GRANT_B, revision: 1 }]);
});

test("a double press sends one request, and nothing dismisses the dialog while it is in flight", async ({
  page,
}) => {
  const held = gate();
  const fake = await setup(page, (f) => {
    f.onLeave = (grant) => {
      // Answered by hand: the owner ends the grant, and the answer is held until the test lets go.
      f.remote = f.remote.filter((r) => r.grant_id !== grant);
      return { hold: held.promise, json: { left: true } };
    };
  });
  const pane = await openPane(page, GRANT_A);
  const dialog = await ask(page, await leaveIn(page, pane), API_TITLE);
  await dialog.getByTestId("leave-confirm").dblclick();
  await expect(dialog.getByTestId("leave-confirm")).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await painted(page);
  await expect(dialog).toBeVisible();
  await onShare(page, GRANT_A); // not shown as left before the owner's answer arrived
  expect(fake.leaves).toHaveLength(1);
  held.open();
  await onSessionList(page);
  expect(fake.leaves).toHaveLength(1);
});
