import { expect, test, type Locator, type Page } from "@playwright/test";
import { TOUCH_FLOOR } from "./touchTarget";
import { settingsPath } from "../src/routes/settingsTabs";
import { MAP_PATH } from "../src/lib/routes";

// Share an API session with a paired instance, view only (#1246 P4a, #2055), in a real browser on
// both projects, against a small in-test model of `routes/federation.py`'s grant and remote
// routes. Three surfaces: the owner's dialog, the receiving side (rows, map node, the read-only
// pane) and Settings → Peers.
//
// What is asserted here is what a browser alone can show: that a sheet keeps its actions under a
// scrolling body, that a request the pane no longer wants is really aborted, that markup a peer
// sent stays text and an image it named is never asked for.
//
// Renewal while the pane is open is NOT here, on purpose: the client never retries a 404. The
// server re-resolves a refusal (one mirror refresh, one retry) before it answers, so a renewed
// grant never reaches the browser as a 404 at all: `tests/test_federation_remote.py`
// (`test_a_grant_renewed_on_the_owner_keeps_a_read_working`). This spec covers the FINAL 404.

import {
  ID2,
  KEY,
  URL_PATH,
  TITLE,
  TITLE2,
  GPU,
  RENDER,
  LAPTOP,
  OLD,
  GRANT_A,
  GRANT_B,
  GRANT_OFF,
  CSRF,
  DISCLOSURE,
  DISCLOSURE_DETAIL,
  NOW,
  DAY,
  date,
  peer,
  turn,
  snapshot,
  QUIET,
  MOVED,
  OFFLINE,
  GONE,
  RENDER_TURNS,
  FakeShare,
  setup,
  isMobile,
  showList,
  SHARE_PEER,
  openDialog,
  noHorizontalOverflow,
  controlsAtLeast44,
  openPane,
  gate,
  painted,
} from "./shareFixture";

// =================================================================================================
// The owner's side
// =================================================================================================

test("the ⋯ menu offers Share with a peer… directly under Share publicly…", async ({ page }) => {
  await setup(page);
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  const pane = page.getByTestId("structured-pane");
  const trigger = pane.getByTestId("head-actions-menu");
  const more = pane.getByRole("button", { name: "More session actions" });
  // Whatever form the pane's menu has here (this spec does not say which): read the order of the
  // session's actions from it, folded and inline together.
  const inline = await pane.locator("[data-head-action]").evaluateAll((els) =>
    els.map((e) => e.getAttribute("aria-label")),
  );
  let folded: (string | null)[] = [];
  if ((await trigger.isVisible()) || (await more.isVisible())) {
    await ((await trigger.isVisible()) ? trigger : more).click();
    const items = page.getByRole("menuitem");
    await expect(items.first()).toBeVisible();
    folded = await items.evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
    await expect(page.getByRole("menuitem", { name: SHARE_PEER })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: SHARE_PEER })).toContainText("Share with a peer…");
  }
  const order = [...inline, ...folded];
  const at = order.indexOf(SHARE_PEER);
  expect(at).toBeGreaterThan(0);
  expect(order[at - 1]).toBe("Share this session publicly");
});

test("share with a peer: the dialog says what leaves, and the share is listed", async ({ page }) => {
  const fake = await setup(page);
  const { dialog } = await openDialog(page);

  await expect(dialog.getByText("SESSION", { exact: true })).toBeVisible();
  await expect(dialog.getByText(TITLE)).toBeVisible();
  // Trusted peers with their connection state; the revoked one is not offered.
  const peers = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio");
  await expect(peers).toHaveCount(3);
  await expect(peers.nth(0)).toHaveText(/gpu-box\s*connected/);
  await expect(peers.nth(0)).toHaveAttribute("aria-checked", "true");
  await expect(peers.nth(2)).toHaveText(/laptop\s*offline · last seen 2 h ago/);
  await expect(dialog.getByText("old-desktop")).toHaveCount(0);
  // Green only for a connected peer.
  const stateColour = (i: number) =>
    peers
      .nth(i)
      .getByTestId("share-peer-state")
      .locator("span")
      .last()
      .evaluate((el) => getComputedStyle(el).color);
  expect(await stateColour(0)).not.toBe(await stateColour(2));

  // View is the level; Suggest and Send exist, disabled, tagged "later".
  const access = dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio");
  await expect(access.nth(0)).toHaveAttribute("aria-checked", "true");
  await expect(access.nth(0)).toContainText("View");
  for (const [i, name] of [
    [1, "Suggest"],
    [2, "Send"],
  ] as const) {
    await expect(access.nth(i)).toBeDisabled();
    await expect(access.nth(i)).toContainText(name);
    await expect(access.nth(i)).toContainText(/later/i);
  }
  // Expiry: 1, 7, 30, 90 days; 7 unless changed.
  const expiry = dialog.getByRole("radiogroup", { name: "Expires" }).getByRole("radio");
  await expect(expiry).toHaveText(["1 day", "7 days", "30 days", "90 days"]);
  await expect(expiry.nth(1)).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByTestId("share-peer-hint")).toHaveText(
    `Ends ${date(NOW() + 7 * DAY)}. You can revoke it before then. It also ends when this session is archived or the peer is revoked.`,
  );
  // The disclosure is on screen without any toggle, word for word.
  await expect(dialog.getByTestId("share-peer-disclosure")).toHaveText(DISCLOSURE + DISCLOSURE_DETAIL);
  await expect(dialog.getByTestId("share-peer-disclosure")).toBeVisible();

  // An offline peer can be granted access, and the dialog says what that means.
  await peers.nth(2).click();
  await expect(dialog.getByTestId("share-peer-offline")).toHaveText(
    "laptop is offline. It sees the session when it next connects.",
  );
  await peers.nth(1).click();
  await expect(dialog.getByTestId("share-peer-offline")).toHaveCount(0);

  await dialog.getByRole("button", { name: "Share", exact: true }).click();
  const row = dialog.getByTestId("share-peer-grant");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("render-box");
  await expect(row).toContainText(/view/i);
  await expect(row).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  await expect(dialog.getByRole("heading", { name: "Shared with · 1" })).toBeVisible();
  await expect(dialog.getByTestId("share-peer-done")).toHaveText(
    `Shared with render-box until ${date(NOW() + 7 * DAY)}.`,
  );
  expect(fake.writes).toEqual([
    {
      method: "POST",
      path: "/grants",
      body: { session_key: KEY, peer_id: RENDER, expires_in: 7 * DAY, level: "view" },
      csrf: CSRF,
    },
  ]);
  await noHorizontalOverflow(page);
});

test("sharing again with the same peer changes the expiry of the existing grant", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.grants = [f.grant(GPU, { grant_id: GRANT_A, revision: 4, expires_at: NOW() + 2 * DAY })];
  });
  const { dialog } = await openDialog(page);
  await expect(dialog.getByTestId("share-peer-grant")).toContainText(`until ${date(NOW() + 2 * DAY)}`);
  await expect(dialog.getByTestId("share-peer-existing")).toHaveText(
    `Already shared with gpu-box until ${date(NOW() + 2 * DAY)}. Sharing again changes when it ends.`,
  );
  await dialog.getByRole("radio", { name: "30 days" }).click();
  await dialog.getByRole("button", { name: "Share", exact: true }).click();
  await expect(dialog.getByTestId("share-peer-grant")).toContainText(`until ${date(NOW() + 30 * DAY)}`);
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1); // never a second one
  expect(fake.writes).toEqual([
    {
      method: "PATCH",
      path: `/grants/${GRANT_A}`,
      body: { expires_in: 30 * DAY, revision: 4 },
      csrf: CSRF,
    },
  ]);
});

test("revoke ends a share from the same dialog", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.grants = [f.grant(GPU, { grant_id: GRANT_A }), f.grant(LAPTOP, { grant_id: GRANT_B })];
  });
  const { dialog } = await openDialog(page);
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(2);
  const revoke = dialog.getByRole("button", { name: "Revoke gpu-box" });
  // Red is Revoke's alone: the button's ink is the danger text, unlike Cancel's.
  const ink = (l: Locator) => l.evaluate((el) => getComputedStyle(el).color);
  expect(await ink(revoke)).not.toBe(await ink(dialog.getByRole("button", { name: "Cancel" })));
  await revoke.click();
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1);
  await expect(dialog.getByTestId("share-peer-grant")).toContainText("laptop");
  await expect(dialog.getByRole("heading", { name: "Shared with · 1" })).toBeVisible();
  expect(fake.writes).toEqual([
    { method: "POST", path: `/grants/${GRANT_A}/revoke`, body: null, csrf: CSRF },
  ]);
});

test("no paired instances: the dialog says so and links to Settings → Peers", async ({ page }) => {
  await setup(page, (f) => {
    f.peers = [peer(OLD, "old-desktop", { state: "revoked" })];
  });
  const { dialog } = await openDialog(page);
  const none = dialog.getByTestId("share-peer-none");
  await expect(none).toContainText("No paired instances yet.");
  await expect(none).toContainText("A session can be shared with an instance this one is paired with.");
  await expect(dialog.getByRole("button", { name: "Share", exact: true })).toBeDisabled();
  await expect(dialog.getByRole("radiogroup")).toHaveCount(0);
  const link = none.getByRole("link", { name: "Open Settings → Peers" });
  if (isMobile(page)) await controlsAtLeast44(dialog);
  await link.click();
  await expect(page).toHaveURL(new RegExp(`${settingsPath("peers")}$`));
  await expect(dialog).toHaveCount(0);
});

test("a refusal (the 200-grant bound) is shown in the dialog with the server's words", async ({ page }) => {
  const detail = "this peer already holds 200 shared sessions — revoke one first";
  const fake = await setup(page, (f) => {
    f.refuse["POST /grants"] = { status: 409, detail };
  });
  const { dialog } = await openDialog(page);
  await dialog.getByRole("button", { name: "Share", exact: true }).click();
  await expect(dialog.getByTestId("share-peer-error")).toHaveText(detail);
  await expect(dialog.getByTestId("share-peer-error")).toHaveAttribute("role", "alert");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(0);
  // It can be tried again, and then it goes through.
  await dialog.getByRole("button", { name: "Share", exact: true }).click();
  await expect(dialog.getByTestId("share-peer-grant")).toHaveCount(1);
  await expect(dialog.getByTestId("share-peer-error")).toHaveCount(0);
  expect(fake.writes.map((w) => w.method)).toEqual(["POST", "POST"]);
});

test("the dialog holds focus, and gives it back to the control that opened it", async ({ page }) => {
  await setup(page, (f) => {
    f.grants = [f.grant(GPU, { grant_id: GRANT_A })];
  });
  const { dialog, opener } = await openDialog(page);
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  // Tab never leaves the dialog, in either direction.
  for (const key of [...Array(14).fill("Tab"), ...Array(5).fill("Shift+Tab")]) {
    await page.keyboard.press(key);
    expect(
      await dialog.evaluate((d) => d.contains(document.activeElement)),
      `focus after ${key}`,
    ).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  if (isMobile(page))
    await expect(page.getByTestId("structured-pane").getByTestId("head-actions-menu")).toBeFocused();
});

test("on a phone the dialog is a sheet: the body scrolls, Share and Revoke stay reachable", async ({
  page,
}) => {
  test.skip(!isMobile(page), "the sheet is the ≤800px form");
  await setup(page, (f) => {
    f.grants = [
      f.grant(GPU, { grant_id: GRANT_A }),
      f.grant(RENDER, { grant_id: GRANT_B }),
      f.grant(LAPTOP, { grant_id: GRANT_OFF }),
    ];
  });
  // 390 px: the narrowest phone the boards were drawn for.
  await page.setViewportSize({ width: 390, height: 760 });
  const { dialog } = await openDialog(page);
  const body = dialog.getByTestId("share-peer-body");
  const share = dialog.getByRole("button", { name: "Share", exact: true });
  const cancel = dialog.getByRole("button", { name: "Cancel" });

  const geo = async () =>
    dialog.evaluate((d) => {
      const b = d.querySelector<HTMLElement>('[data-testid="share-peer-body"]')!;
      const go = d.querySelector<HTMLElement>('[data-testid="share-peer-go"]')!.getBoundingClientRect();
      const box = d.getBoundingClientRect();
      return {
        dialogScroll: d.scrollTop,
        dialogOverflow: getComputedStyle(d).overflowY,
        bodyOverflow: getComputedStyle(b).overflowY,
        bodyScrolls: b.scrollHeight > b.clientHeight + 1,
        bodyTop: b.scrollTop,
        bodyBottom: b.getBoundingClientRect().bottom,
        shareTop: go.top,
        shareBottom: go.bottom,
        floor: box.bottom,
        viewport: window.innerHeight,
        flush: Math.abs(box.bottom - window.innerHeight) < 1 && Math.abs(box.left) < 1,
      };
    });

  // A sheet flush to the floor whose BODY is the scroller: the sheet itself never scrolls.
  const top = await geo();
  expect(top.flush).toBe(true);
  expect(top.dialogOverflow).toBe("hidden");
  expect(top.bodyOverflow).toBe("auto");
  expect(top.bodyScrolls).toBe(true);
  expect(top.shareBottom).toBeLessThanOrEqual(top.viewport);
  expect(top.shareTop).toBeGreaterThanOrEqual(top.bodyBottom - 1); // under the body, not over it
  await expect(share).toBeInViewport({ ratio: 1 });
  await expect(cancel).toBeInViewport({ ratio: 1 });

  // Scroll the body to its end: the existing shares come up, and Share has not moved.
  await body.evaluate((b) => b.scrollTo(0, b.scrollHeight));
  const end = await geo();
  expect(end.bodyTop).toBeGreaterThan(0);
  expect(end.dialogScroll).toBe(0);
  expect(end.shareTop).toBe(top.shareTop);
  const lastRevoke = dialog.getByRole("button", { name: "Revoke laptop" });
  await expect(lastRevoke).toBeInViewport({ ratio: 1 });
  await expect(share).toBeInViewport({ ratio: 1 });
  const revokeBox = (await lastRevoke.boundingBox())!;
  expect(revokeBox.y + revokeBox.height).toBeLessThanOrEqual(end.bodyBottom + 1); // not under the row
  // Both are really pressable there: nothing covers either.
  await lastRevoke.click({ trial: true });
  await share.click({ trial: true });

  await controlsAtLeast44(dialog);
  await noHorizontalOverflow(page);
  const wide = await dialog.evaluate((d) =>
    [...d.querySelectorAll<HTMLElement>("*")]
      .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
      .map((el) => el.className),
  );
  expect(wide).toEqual([]);
});

test("on a wide screen the dialog scrolls as one and its actions sit above the list", async ({ page }) => {
  test.skip(isMobile(page), "the ≤800px sheet has its own test");
  await setup(page, (f) => {
    f.grants = [f.grant(GPU, { grant_id: GRANT_A })];
  });
  const { dialog } = await openDialog(page);
  const share = (await dialog.getByRole("button", { name: "Share", exact: true }).boundingBox())!;
  const list = (await dialog.getByRole("heading", { name: "Shared with · 1" }).boundingBox())!;
  const disclosure = (await dialog.getByTestId("share-peer-disclosure").boundingBox())!;
  expect(share.y).toBeGreaterThan(disclosure.y + disclosure.height - 1);
  expect(list.y).toBeGreaterThan(share.y + share.height - 1);
});

// =================================================================================================
// The receiving side
// =================================================================================================

test("remote rows: title, peer badge, view only and the end date, or offline; no menu", async ({ page }) => {
  await setup(page);
  await page.goto(URL_PATH);
  const list = await showList(page);
  const remote = list.getByTestId("remote-row");
  await expect(remote).toHaveCount(3);
  await expect(remote.nth(0)).toContainText("Tune the render queue");
  await expect(remote.nth(0).getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(remote.nth(0).getByTestId("remote-row-meta")).toHaveText(
    `view only · until ${date(NOW() + 7 * DAY)}`,
  );
  await expect(remote.nth(2)).toContainText("Field recording index");
  await expect(remote.nth(2).getByTestId("peer-badge")).toHaveText("laptop");
  await expect(remote.nth(2).getByTestId("remote-row-meta")).toHaveText("offline");
  // The badge is a dashed hairline in neutral ink: an origin, not a state.
  const badge = await remote
    .nth(0)
    .getByTestId("peer-badge")
    .evaluate((el) => {
      const cs = getComputedStyle(el);
      return { style: cs.borderTopStyle, ink: cs.color, text: getComputedStyle(document.body).color };
    });
  expect(badge.style).toBe("dashed");
  // A remote row is a link and nothing else: no ⋯, no LED, no button of any kind.
  await expect(remote.locator("button")).toHaveCount(0);
  await expect(remote.locator(".hud-led")).toHaveCount(0);
  // The local rows keep theirs: the two kinds are not the same component.
  const localRows = list.locator("li").filter({ has: page.locator(`a[href^="/s/"]`) });
  await expect(localRows).toHaveCount(2);
  if (isMobile(page)) {
    for (let i = 0; i < 3; i++) {
      const box = (await remote.nth(i).locator("a").boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  }
});

test("an expiry no browser can make a date of drops its row and breaks nothing (#2055 review)", async ({
  page,
}) => {
  // A peer once could list `1e300`: finite, accepted, and `RangeError: Invalid time value` when
  // the row formatted it, outside the pane's error boundary, so the whole app went down.
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await setup(page, (fake) => {
    fake.remote = [
      { ...fake.row(GPU, "gpu-box", GRANT_A, "Tune the render queue", true), expires_at: 1e300 },
      fake.row(GPU, "gpu-box", GRANT_B, "Color pipeline notes", true, 30),
    ];
  });
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  const list = await showList(page);
  const remote = list.getByTestId("remote-row");
  await expect(remote).toHaveCount(1);
  await expect(remote).toContainText("Color pipeline notes");
  await expect(list.locator("li").filter({ has: page.locator(`a[href^="/s/"]`) })).toHaveCount(2);
  expect(errors).toEqual([]);
});

test("a remote row opens the read-only pane: no composer, no session actions", async ({ page }) => {
  const fake = await setup(page);
  await page.goto(URL_PATH);
  const list = await showList(page);
  await list.getByTestId("remote-row").first().locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_A}$`));
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-title")).toHaveText("Tune the render queue");
  await expect(pane.getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(pane.getByTestId("remote-banner")).toContainText(
    `Shared by gpu-box · view only · until ${date(NOW() + 7 * DAY)}`,
  );
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
  // The turns, drawn by the public viewer's own components.
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByText("The render queue stalls when two large jobs land together.")).toBeVisible();
  await expect(pane.getByText("Done. The lock is per job now and the queue tests pass.")).toBeVisible();
  await expect(pane.getByText("diagram.png")).toBeVisible(); // an image shows as its name only
  await expect(pane.locator("img")).toHaveCount(0);
  await expect(pane.getByTestId("share-activity").first()).toContainText("2 commands & tools");
  // In the composer's place.
  await expect(pane.getByTestId("remote-view-only")).toHaveText(
    "View only. Ask gpu-box's operator for more access.",
  );
  // Nothing to type into and nothing to act with: this is not the local pane.
  await expect(pane.locator("textarea, input, [contenteditable='true']")).toHaveCount(0);
  await expect(pane.getByRole("button")).toHaveCount(0);
  await expect(page.getByTestId("structured-pane")).toHaveCount(0);
  await expect(page.getByTestId("head-actions-menu")).toHaveCount(0);
  await expect(page.locator("[data-head-action]")).toHaveCount(0);
  // The dashed outline of something this instance cannot act on.
  expect(await pane.evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe("dashed");
  // Every read went to the remote routes, by peer id and grant id, and the hold is 3 s.
  expect(fake.reads[0]).toMatchObject({ kind: "snapshot", peer: GPU, grant: GRANT_A });
  await expect.poll(() => fake.reads.some((r) => r.kind === "events")).toBe(true);
  expect(fake.reads.find((r) => r.kind === "events")!.search).toMatch(/[?&]wait=3(&|$)/);
  await noHorizontalOverflow(page);
});

test("nothing from a peer is kept in storage", async ({ page }) => {
  await setup(page);
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  const stored = await page.evaluate(() =>
    JSON.stringify([{ ...localStorage }, { ...sessionStorage }]),
  );
  for (const secret of ["render queue", "per-job lock", "scheduler", "gpu-box", GRANT_A, GPU])
    expect(stored, `storage holds "${secret}"`).not.toContain(secret);
});

test("503: the pane drops its turns and says the peer is not connected; it fills again when the peer is back", async ({
  page,
}) => {
  let offline = false;
  const fake = await setup(page, (f) => {
    f.onRead = (kind) =>
      offline ? OFFLINE : kind === "snapshot" ? { json: snapshot(RENDER_TURNS) } : QUIET;
  });
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");

  offline = true; // the next read (the events poll) is refused: the channel is down
  await expect(pane.getByTestId("remote-empty")).toHaveText(
    "gpu-box is not connected. Showing nothing until it is back.",
  );
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(page.getByText("The render queue stalls")).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("offline");
  await expect(pane.getByTestId("remote-view-only")).toHaveCount(0);
  // Grey, not red and not green: offline is a quiet state.
  const led = await pane.getByTestId("remote-state").locator(".hud-led").getAttribute("class");
  expect(led).toContain("idle");

  // It keeps asking, and never shows a turn while the answer is 503.
  const asked = fake.reads.length;
  await expect.poll(() => fake.reads.length, { timeout: 10_000 }).toBeGreaterThan(asked);
  await expect(pane.locator("[data-turn]")).toHaveCount(0);

  offline = false;
  await expect(pane.locator("[data-turn]")).toHaveCount(2, { timeout: 10_000 });
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
  await expect(pane.getByTestId("remote-view-only")).toBeVisible();
});

test("a final 404: the neutral unavailable state, and the row is gone on the next list read", async ({
  page,
}) => {
  let ended = false;
  const fake = await setup(page, (f) => {
    f.onRead = (kind) =>
      ended ? GONE : kind === "snapshot" ? { json: snapshot(RENDER_TURNS) } : QUIET;
  });
  await page.goto(URL_PATH);
  let list = await showList(page);
  await expect(list.getByTestId("remote-row")).toHaveCount(3);
  await list.getByTestId("remote-row").first().locator("a").click();
  const pane = page.getByTestId("remote-pane");
  await expect(pane.locator("[data-turn]")).toHaveCount(2);

  // The share ends on the owner. The server's read refreshed its mirror before it said 404, so
  // the list it serves from now on has no such row.
  const listReads = fake.listReads;
  fake.remote = fake.remote.filter((r) => r.grant_id !== GRANT_A);
  ended = true;
  await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(page.getByText("The render queue stalls")).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("unavailable");
  // Neutral: it does not say revoked or expired (this side cannot know), and it is not an alarm.
  await expect(pane).not.toContainText(/revoked|expired/i);
  await expect(pane.locator('[role="alert"]')).toHaveCount(0);
  await expect(pane.getByTestId("remote-view-only")).toHaveCount(0);
  // The pane asked for the list again, and the row left with that read.
  await expect.poll(() => fake.listReads).toBeGreaterThan(listReads);
  list = await showList(page);
  await expect(list.getByTestId("remote-row")).toHaveCount(2);
  await expect(list.getByText("Tune the render queue")).toHaveCount(0);
  // Final: nothing is asked of that share again.
  const asked = fake.reads.length;
  await page.waitForTimeout(3_500);
  expect(fake.reads.length).toBe(asked);
});

test("a route whose ids are not the server's shape asks nothing and shows unavailable", async ({ page }) => {
  const fake = await setup(page);
  await page.goto("/remote/not-a-peer/x");
  await expect(page.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
  expect(fake.reads).toEqual([]);
});

/** A pane on GRANT_A that is live with two turns, whose SECOND snapshot (asked for when the
 *  events read reports movement) is held by the test: the slow success of the fence tests. */
async function paneWithHeldSnapshot(page: Page, other?: (f: FakeShare) => void) {
  const held = gate();
  const state = { mode: "live" as "live" | "offline" | "gone" };
  const aborted: string[] = [];
  page.on("requestfailed", (r) => {
    if (/\/remote\/.+\/snapshot$/.test(new URL(r.url()).pathname)) aborted.push(r.url());
  });
  const fake = await setup(page, (f) => {
    other?.(f);
    f.onRead = (kind, grant, _url, n) => {
      if (grant !== GRANT_A)
        return kind === "snapshot"
          ? { json: snapshot([turn("bbbbbbbb-0000-4000-8000-000000000001", "Which LUT is in use?", "ACES 1.3, set in grade.toml.")]) }
          : QUIET;
      if (kind === "snapshot" && n === 2)
        return {
          hold: held.promise,
          label: "late",
          json: snapshot([...RENDER_TURNS, turn("aaaaaaaa-0000-4000-8000-00000000000f", "LATE QUESTION", "LATE ANSWER")], {
            omitted_turns: 2,
          }),
        };
      if (state.mode === "offline") return OFFLINE;
      if (state.mode === "gone") return GONE;
      if (kind === "snapshot") return { json: snapshot(RENDER_TURNS, { omitted_turns: 2 }) };
      if (kind === "events") return n === 1 ? MOVED : QUIET;
      return OFFLINE; // history: the peer drops while older turns are asked for
    };
  });
  await page.goto(URL_PATH);
  const list = await showList(page);
  await list.getByTestId("remote-row").first().locator("a").click();
  const pane = page.getByTestId("remote-pane");
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  // The second snapshot is on its way and held.
  await expect.poll(() => fake.reads.filter((r) => r.kind === "snapshot" && r.grant === GRANT_A).length).toBe(2);
  return { fake, pane, held, state, aborted };
}

test("a slow success that arrives after the pane cleared is discarded", async ({ page }) => {
  const { fake, pane, held, state, aborted } = await paneWithHeldSnapshot(page);
  // The pane clears: asking for older turns is answered 503 (the peer dropped).
  state.mode = "offline";
  await pane.getByTestId("remote-older").click();
  await expect(pane.getByTestId("remote-empty")).toHaveText(
    "gpu-box is not connected. Showing nothing until it is back.",
  );
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  // Now the held success is sent. The pane gave that request up when it cleared...
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  expect(aborted.length).toBeGreaterThanOrEqual(1);
  // ...and nothing of it is ever shown: the pane still shows nothing.
  await expect(page.getByText("LATE ANSWER")).toHaveCount(0);
  await expect(page.getByText("LATE QUESTION")).toHaveCount(0);
  await expect(pane.locator("[data-turn]")).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("offline");
  // It stays that way while the peer is away (the retries are 503 too).
  const asked = fake.reads.length;
  await expect.poll(() => fake.reads.length, { timeout: 10_000 }).toBeGreaterThan(asked);
  await expect(page.getByText("LATE ANSWER")).toHaveCount(0);
});

test("a slow success that arrives after navigating away is discarded", async ({ page }) => {
  const { fake, held, aborted } = await paneWithHeldSnapshot(page);
  // Leave for a local session, inside the app (no reload: the same document keeps running).
  const list = await showList(page);
  await list.locator(`a[href="${URL_PATH}"]`).click();
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.getByTestId("remote-pane")).toHaveCount(0);
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  // The pane's request was given up when it left, and nothing of a peer is on the page.
  expect(aborted.length).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("LATE ANSWER")).toHaveCount(0);
  await expect(page.getByText("The render queue stalls")).toHaveCount(0);
  // ...and nothing asks that share anything any more.
  const asked = fake.reads.length;
  await page.waitForTimeout(2_000);
  expect(fake.reads.length).toBe(asked);
});

test("a slow success for one shared session never lands in another's pane", async ({ page }) => {
  const { fake, pane, held, aborted } = await paneWithHeldSnapshot(page);
  // Switch to the other session gpu-box shares: the same route, another grant.
  const list = await showList(page);
  await list.getByTestId("remote-row").nth(1).locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_B}$`));
  await expect(pane.getByTestId("remote-title")).toHaveText("Color pipeline notes");
  await expect(pane.getByText("ACES 1.3, set in grade.toml.")).toBeVisible();
  // Nothing of the first session survived the switch, even before its late answer.
  await expect(pane.getByText("The render queue stalls")).toHaveCount(0);
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  expect(aborted.length).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("LATE ANSWER")).toHaveCount(0);
  await expect(pane.getByText("The render queue stalls")).toHaveCount(0);
  await expect(pane.locator("[data-turn]")).toHaveCount(1);
  await expect(pane.getByTestId("remote-title")).toHaveText("Color pipeline notes");
});

test("a long-poll event refreshes the turns", async ({ page }) => {
  let moved = false;
  let revision = 5;
  const turns = [...RENDER_TURNS];
  const fake = await setup(page, (f) => {
    f.onRead = (kind) => {
      if (kind === "snapshot") return { json: snapshot(turns, { revision, event_cursor: revision }) };
      if (kind === "events" && moved) {
        moved = false;
        return { json: { link: "L".repeat(24), revision, next_cursor: revision, events: [{ cursor: revision, kind: "text" }] } };
      }
      return { json: { link: "L".repeat(24), revision, next_cursor: revision, events: [] } };
    };
  });
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  const snapshots = () => fake.reads.filter((r) => r.kind === "snapshot").length;
  expect(snapshots()).toBe(1);
  // A quiet journal reads the snapshot once: the events read is what the pane waits on.
  await expect.poll(() => fake.reads.filter((r) => r.kind === "events").length).toBeGreaterThanOrEqual(2);
  expect(snapshots()).toBe(1);

  // The owner's session moves on. The event carries a cursor and a kind, never the text.
  turns.push(turn("aaaaaaaa-0000-4000-8000-000000000003", "Now do the same for the bake queue.", "The bake queue has the same lock; changed there too."));
  revision = 6;
  moved = true;
  await expect(pane.getByText("The bake queue has the same lock; changed there too.")).toBeVisible();
  await expect(pane.locator("[data-turn]")).toHaveCount(3);
  expect(snapshots()).toBe(2);
  // Every events read was a 3 s hold, after the cursor the pane holds.
  for (const r of fake.reads.filter((x) => x.kind === "events")) {
    expect(r.search).toMatch(/[?&]wait=3(&|$)/);
    expect(r.search).toMatch(/[?&]after=\d+(&|$)/);
  }
});

test("older turns page in, oldest first, and a cut turn says so", async ({ page }) => {
  const older = [
    turn("aaaaaaaa-0000-4000-8000-0000000000a1", "First: what does the queue do today?", "It batches jobs by size."),
    turn("aaaaaaaa-0000-4000-8000-0000000000a2", "And where is the lock taken?", "In Scheduler.take_batch.", {
      truncated: true,
      tools: [{ name: "read", outcome: "ok", summary: "queue/scheduler.py" }],
      tools_omitted: 3,
    }),
  ];
  const fake = await setup(page, (f) => {
    f.onRead = (kind) =>
      kind === "snapshot"
        ? { json: snapshot(RENDER_TURNS, { omitted_turns: 2 }) }
        : kind === "events"
          ? QUIET
          : { json: { link: "L".repeat(24), turns: older, before: older[0].turn_id, has_more: false } };
  });
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  const more = pane.getByTestId("remote-older");
  await expect(more).toHaveText("Show earlier messages");
  if (isMobile(page)) {
    const box = (await more.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await more.click();
  await expect(pane.locator("[data-turn]")).toHaveCount(4);
  expect(await pane.locator("[data-turn]").evaluateAll((els) => els.map((e) => e.getAttribute("data-turn")))).toEqual([
    older[0].turn_id,
    older[1].turn_id,
    RENDER_TURNS[0].turn_id,
    RENDER_TURNS[1].turn_id,
  ]);
  // It asked for the page before the oldest turn it held.
  const asked = fake.reads.find((r) => r.kind === "history")!;
  expect(asked.search).toBe(`?before=${RENDER_TURNS[0].turn_id}`);
  await expect(more).toHaveCount(0); // has_more: false
  // The turn its sender cut to fit carries the marker; the others do not.
  const cut = pane.locator(`[data-turn="${older[1].turn_id}"]`);
  await expect(cut.getByTestId("turn-truncated")).toHaveText("Part of this turn was cut to fit.");
  await expect(cut.getByTestId("share-activity")).toContainText("Latest 1 command & tool");
  await expect(pane.getByTestId("turn-truncated")).toHaveCount(1);
});

test("remote rows are untouched by the local list's filters and actions", async ({ page }) => {
  const fake = await setup(page);
  const sessionWrites: string[] = [];
  page.on("request", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/sessions") && r.method() !== "GET") sessionWrites.push(r.url() + (r.postData() ?? ""));
  });
  await page.goto(URL_PATH);
  const list = await showList(page);
  const remote = list.getByTestId("remote-row");
  const local = list.locator(`a[href^="/s/"]`);
  await expect(remote).toHaveCount(3);
  await expect(local).toHaveCount(2);

  // A title search narrows the LOCAL list. A remote title that matches is not "found" by it and
  // one that does not is not hidden by it: the rows are simply not part of that list.
  const search = list.getByRole("searchbox", { name: "Search sessions" });
  if (!(await search.isVisible())) await list.getByRole("button", { name: /filter/i }).first().click();
  await search.fill("nightly");
  await expect(local).toHaveCount(1);
  await expect(remote).toHaveCount(3);
  await search.fill("zzzz no such session");
  await expect(local).toHaveCount(0);
  await expect(list.getByText("No sessions match — clear filters.")).toBeVisible();
  await expect(remote).toHaveCount(3);
  await search.fill("render queue"); // matches a REMOTE title only
  await expect(local).toHaveCount(0);
  await expect(remote).toHaveCount(3);
  await search.fill("");
  await expect(local).toHaveCount(2);

  // The archived tab: no remote row is ever "archived" or hidden by it.
  await list.getByRole("tab", { name: "Archived" }).click();
  await expect(local).toHaveCount(0);
  await expect(remote).toHaveCount(3);
  await list.getByRole("tab", { name: "Active" }).click();
  await expect(local).toHaveCount(2);

  // Selection: the open session is a local row; no remote row is ever the selected one for it.
  await expect(list.locator(`a[href="${URL_PATH}"]`)).toHaveAttribute("aria-current", "page");
  await expect(remote.locator('a[aria-current="page"]')).toHaveCount(0);
  // Row actions exist per LOCAL row only: a remote row offers none to select or to act with.
  await expect(list.getByRole("button", { name: /^Session actions/ })).toHaveCount(2);
  await expect(remote.getByRole("button")).toHaveCount(0);
  await expect(remote.locator("input")).toHaveCount(0);

  // Nothing of a peer ever went to the session API: not as a filter, a key or a body.
  for (const q of fake.sessionQueries) {
    expect(q).not.toContain("peer");
    expect(q).not.toContain(GRANT_A);
  }
  expect(sessionWrites).toEqual([]);
});

test("the map draws a shared session as its own node: dashed, badged, no ⋯, opens the pane", async ({
  page,
}) => {
  await setup(page);
  await page.goto(MAP_PATH);
  const nodes = page.getByTestId("remote-node");
  await expect(nodes).toHaveCount(3);
  const node = nodes.first();
  await expect(node).toContainText("Tune the render queue");
  await expect(node.getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(node).toContainText(/view only/i);
  await expect(node).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  await expect(nodes.nth(2)).toContainText(/offline/i);
  expect(await node.evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe("dashed");
  // No ⋯, and it is not a unit: no state LED, no engine callsign.
  await expect(node.locator("button")).toHaveCount(0);
  await expect(node.locator("[data-chip-menu]")).toHaveCount(0);
  await expect(node.locator(".tr-ov-dot")).toHaveCount(0);
  // It cannot be moved, so it cannot be dropped on a project or a group.
  const wrapper = page.locator(`.react-flow__node[data-id="remote:${GPU}/${GRANT_A}"]`);
  await expect(wrapper).toHaveCount(1);
  await expect(wrapper).not.toHaveClass(/draggable/);
  // A right-click opens no session menu for it.
  await node.click({ button: "right" });
  await expect(page.getByRole("menu")).toHaveCount(0);
  // A click opens the read-only pane's own route, never a map window.
  await node.click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_A}$`));
  await expect(page.getByTestId("remote-pane").locator("[data-turn]")).toHaveCount(2);
});

test("hostile text from a peer is text: no markup, no script, no dangerous link, no image request", async ({
  page,
}) => {
  const evil: string[] = [];
  const dialogs: string[] = [];
  page.on("dialog", (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });
  const TITLE_X = `<img src=x onerror="window.__pwned=1">Queue <b>notes</b>`;
  const NAME_X = `<script>window.__pwned=2</script>gpu`;
  const reply = [
    "<b id=\"planted\">bold</b> and <script>window.__pwned=3</script>",
    "",
    "A [safe link](https://example.com/docs?a=1) here.",
    "",
    "A [js link](javascript:window.__pwned=4) and a [data link](data:text/html,<script>window.__pwned=5</script>).",
    "",
    "![pixel alt text](https://evil.example/pixel.png)",
    "",
    "<img src=\"https://evil.example/raw.png\" onerror=\"window.__pwned=6\">",
  ].join("\n");
  await setup(page, (f) => {
    f.remote = [f.row(GPU, NAME_X, GRANT_A, TITLE_X, true)];
    f.onRead = (kind) =>
      kind === "snapshot"
        ? {
            json: snapshot([
              turn("aaaaaaaa-0000-4000-8000-0000000000e1", `<u id="planted-q">question</u>`, reply, {
                tools: [{ name: `<i id="planted-tool">read</i>`, outcome: "ok", summary: `<s>x</s>` }],
                attachments: [
                  { name: `<em>shot</em>.png`, mime: "image/png", image: "https://evil.example/handle.png" },
                ],
                author: { name: `<b>visitor</b>`, via: "public_link" },
              }),
            ]),
          }
        : QUIET;
  });
  // Registered after setup: anything asked of that host is recorded and refused.
  await page.route(/evil\.example/, (r) => {
    evil.push(r.request().url());
    return r.abort();
  });
  page.on("request", (r) => {
    if (r.url().includes("evil.example")) evil.push(r.url());
  });

  await page.goto(URL_PATH);
  const list = await showList(page);
  // The list row: the title and the peer's name, as written.
  const row = list.getByTestId("remote-row").first();
  await expect(row).toContainText(TITLE_X);
  await expect(row.getByTestId("peer-badge")).toHaveText(NAME_X);
  await expect(row.locator("img, b, script")).toHaveCount(0);
  await row.locator("a").click();

  const pane = page.getByTestId("remote-pane");
  await expect(pane.locator("[data-turn]")).toHaveCount(1);
  await expect(pane.getByTestId("remote-title")).toHaveText(TITLE_X);
  await expect(pane.getByTestId("remote-banner")).toContainText(NAME_X);
  // Markup in a title, a name, a message, a tool line, an attachment and an author: all text.
  await expect(pane.getByText(`<u id="planted-q">question</u>`)).toBeVisible();
  await pane.getByTestId("share-activity").locator("summary").click();
  await expect(pane.getByText(`<i id="planted-tool">read</i>`)).toBeVisible();
  await expect(pane.getByText(`<em>shot</em>.png`)).toBeVisible();
  await expect(pane.getByText(`<b>visitor</b> · via public link`)).toBeVisible();
  // The reply went through the Markdown policy: raw HTML is shown as written, never parsed.
  await expect(pane.getByText(`<b id="planted">bold</b>`, { exact: false })).toBeVisible();
  for (const id of ["planted", "planted-q", "planted-tool"]) await expect(page.locator(`#${id}`)).toHaveCount(0);
  await expect(pane.locator("script, img, iframe, object, embed, u, s, em")).toHaveCount(0);

  // An https link is a link that opens elsewhere, without a referrer or an opener.
  const safe = pane.getByRole("link", { name: "safe link" });
  await expect(safe).toHaveAttribute("href", "https://example.com/docs?a=1");
  await expect(safe).toHaveAttribute("target", "_blank");
  const rel = (await safe.getAttribute("rel")) ?? "";
  expect(rel.split(/\s+/)).toEqual(expect.arrayContaining(["noopener", "noreferrer"]));
  // javascript: and data: targets are inert text: there is no such link at all.
  await expect(pane.getByRole("link")).toHaveCount(1);
  await expect(pane.locator('a[href^="javascript"], a[href^="data:"]')).toHaveCount(0);
  await expect(pane.getByText("js link", { exact: false })).toBeVisible();
  await expect(pane.getByText("data link", { exact: false })).toBeVisible();
  // A Markdown image shows as its alt text and is never loaded.
  await expect(pane.getByText("pixel alt text", { exact: false })).toBeVisible();
  await expect(pane.locator("img")).toHaveCount(0);

  await painted(page);
  await page.waitForLoadState("networkidle").catch(() => {});
  expect(evil, "a request to the peer-named host").toEqual([]);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  expect(dialogs).toEqual([]);
  await noHorizontalOverflow(page);
});

test("the pane at 390 px: no sideways overflow in any state, and 44 px controls", async ({ page }) => {
  test.skip(!isMobile(page), "the phone layout");
  await page.setViewportSize({ width: 390, height: 760 });
  let mode: "live" | "offline" | "gone" = "live";
  await setup(page, (f) => {
    f.remote = [
      f.row(GPU, "a-rather-long-instance-name-for-a-badge", GRANT_A, "A long shared session title that will not fit on one line of a phone", true),
      ...f.remote.slice(1),
    ];
    f.onRead = (kind) =>
      mode === "offline"
        ? OFFLINE
        : mode === "gone"
          ? GONE
          : kind === "snapshot"
            ? {
                json: snapshot(
                  [
                    ...RENDER_TURNS,
                    turn("aaaaaaaa-0000-4000-8000-0000000000c1", "x".repeat(300), "`" + "y".repeat(300) + "`\n\n" + "z".repeat(300)),
                  ],
                  { omitted_turns: 3 },
                ),
              }
            : QUIET;
  });
  const pane = await openPane(page);
  await expect(pane.locator("[data-turn]")).toHaveCount(3);
  const inside = async () => {
    await noHorizontalOverflow(page);
    const wide = await pane.evaluate((p) =>
      [...p.querySelectorAll<HTMLElement>("header, header *, [role=note], [role=note] *, [data-testid=remote-view-only], [data-testid=remote-empty]")]
        .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 1)
        .map((el) => el.className),
    );
    expect(wide).toEqual([]);
  };
  await inside();
  await controlsAtLeast44(pane);
  // The list with remote rows (the drawer), at the same width.
  const list = await showList(page);
  await expect(list.getByTestId("remote-row")).toHaveCount(3);
  await noHorizontalOverflow(page);
  const rowWide = await list.evaluate((l) =>
    [...l.querySelectorAll<HTMLElement>('[data-testid="remote-row"], [data-testid="remote-row"] *')]
      .filter((el) => el.getBoundingClientRect().right > l.getBoundingClientRect().right + 1)
      .map((el) => el.className),
  );
  expect(rowWide).toEqual([]);
  await list.getByTestId("remote-row").first().locator("a").click();
  mode = "offline";
  await expect(pane.getByTestId("remote-empty")).toContainText("is not connected");
  await inside();
  mode = "gone";
  await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.", {
    timeout: 10_000,
  });
  await inside();
});

// =================================================================================================
// Settings → Peers
// =================================================================================================

test("Settings → Peers lists, per peer, what is shared with it and what it shares here", async ({ page }) => {
  const fake = await setup(page, (f) => {
    f.grants = [
      f.grant(GPU, { grant_id: GRANT_A, title: TITLE }),
      f.grant(GPU, { grant_id: GRANT_B, title: TITLE2, session_key: `codex-api:${ID2}`, expires_at: NOW() + 30 * DAY }),
      f.grant(LAPTOP, { grant_id: GRANT_OFF, title: TITLE }),
    ];
  });
  await page.goto(settingsPath("peers"));
  const row = (id: string) => page.locator(`[data-testid="peer-row"][data-peer="${id}"]`);
  const gpu = row(GPU);
  await expect(gpu).toBeVisible();
  // Collapsed, the row shows the two counts.
  await expect(gpu.getByTestId("peer-shares-counts")).toHaveText("Sessions · 2 shared · 2 received");
  await expect(row(LAPTOP).getByTestId("peer-shares-counts")).toHaveText("Sessions · 1 shared · 1 received");
  await expect(row(RENDER).getByTestId("peer-shares-counts")).toHaveText("Sessions · 0 shared · 0 received");
  // A revoked peer has no shares in either direction, and no disclosure.
  await expect(row(OLD).getByTestId("peer-shares")).toHaveCount(0);
  const toggle = gpu.getByRole("button", { name: /Sessions · 2 shared · 2 received/ });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(gpu.getByTestId("peer-grant").first()).not.toBeInViewport();

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const out = gpu.getByTestId("peer-shares-out");
  await expect(out.getByRole("heading", { name: "Shared with gpu-box · 2" })).toBeVisible();
  const grants = out.getByTestId("peer-grant");
  await expect(grants).toHaveCount(2);
  await expect(grants.nth(0)).toContainText(TITLE);
  await expect(grants.nth(0)).toContainText(/view/i);
  await expect(grants.nth(0)).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  await expect(grants.nth(1)).toContainText(TITLE2);
  await expect(grants.nth(1)).toContainText(`until ${date(NOW() + 30 * DAY)}`);
  await expect(grants.getByRole("button", { name: /^Revoke/ })).toHaveCount(2);

  const inbound = gpu.getByTestId("peer-shares-in");
  await expect(inbound.getByRole("heading", { name: "Shared by gpu-box · 2" })).toBeVisible();
  const received = inbound.getByTestId("peer-received");
  await expect(received).toHaveCount(2);
  await expect(received.nth(0)).toContainText("Tune the render queue");
  await expect(received.nth(0)).toContainText(/view/i);
  await expect(received.nth(0)).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  // Read-only: nothing to press, and the line that says who decides.
  await expect(inbound.getByRole("button")).toHaveCount(0);
  await expect(inbound.getByTestId("peer-shares-readonly")).toHaveText(
    "Read-only here. gpu-box's operator decides how long it stays shared.",
  );
  if (isMobile(page)) await controlsAtLeast44(gpu);
  await noHorizontalOverflow(page);

  // Revoking one share ends that one, here.
  await grants.nth(0).getByRole("button", { name: `Revoke ${TITLE} for gpu-box` }).click();
  await expect(grants).toHaveCount(1);
  await expect(gpu.getByTestId("peer-shares-counts")).toHaveText("Sessions · 1 shared · 2 received");
  await expect(out.getByRole("heading", { name: "Shared with gpu-box · 1" })).toBeVisible();
  expect(fake.writes).toEqual([
    { method: "POST", path: `/grants/${GRANT_A}/revoke`, body: null, csrf: CSRF },
  ]);
  // The other peer's list was not touched.
  await expect(row(LAPTOP).getByTestId("peer-shares-counts")).toHaveText("Sessions · 1 shared · 1 received");
});
