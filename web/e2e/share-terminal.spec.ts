import { expect, test, type Locator, type Page } from "@playwright/test";
import { ACTIONS_MENU, SHARE_PUBLIC, SHARE_PUBLIC_UNAVAILABLE, clickHeadAction } from "./headActions";
import { animationsSettled } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";
import { MAP_PATH } from "../src/lib/routes";
import { remotePath } from "../src/lib/federation";
import {
  CSRF,
  DAY,
  FP_A,
  FP_B,
  type FakeShare,
  GONE,
  GPU,
  GRANT_A,
  GRANT_T,
  GRANT_T2,
  LAPTOP,
  MASK,
  NOW,
  OFFLINE,
  QUIET,
  RENDER_TURNS,
  SCREEN_LINES,
  SCREEN_UNAVAILABLE,
  SETTLING,
  TERM_KEY,
  TERM_TITLE,
  TERM_URL,
  URL_PATH,
  controlsAtLeast44,
  date,
  frame,
  gate,
  isMobile,
  noHorizontalOverflow,
  painted,
  setup,
  setupTerminal,
  showLocal,
  showShared,
  snapshot,
  unchanged,
} from "./shareFixture";

// View a paired instance's TERMINAL as its screen (#1246 P4b, #2060), in a real browser on both
// projects, against the same in-test model of `routes/federation.py` that `share-peer.spec.ts`
// uses (`shareFixture.ts`), extended with the frame read.
//
// What is asserted here is what a browser alone can show: that the limit is on screen when the
// phone sheet opens, that a frame keeps its columns, that a wide one scrolls inside its own box,
// that a dimmed frame is dropped on the clock and the next request really carries no `have`,
// that a request the pane no longer wants is really aborted, that keys go nowhere, and that
// markup in a frame stays text.
//
// Renewal while the pane is open is NOT here: the server re-resolves a refusal (one mirror
// refresh, one retry) before it answers, so a renewed terminal grant never reaches the browser as
// a 404: `tests/test_federation_terminal.py`
// (`test_the_frame_route_keeps_working_for_a_terminal_grant_renewed_on_the_owner`).

const SHARE_TERMINAL = "Share this terminal's screen with a paired instance";
// The limit, in the dialog's words (docs/invariants/federation.md, "The stated limits"): what is
// masked, what is not, and what is never sent at all.
const LEAD =
  "The peer sees this terminal's screen. A secret we know of is hidden when all of it is on one line or it wraps at the screen's edge.";
const UNPROTECTED =
  "A secret that is cut off, one a program lays out across lines itself, or one we don't know about is not hidden.";
const NOT_SHOWN =
  "No screen is sent after a control we can't redraw exactly, such as a backspace in a shell, or while the screen shows any character outside plain Latin, Greek and Cyrillic text, punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic, Indic and similar scripts, or combining characters. An agent that draws emoji is often not viewable.";
const DETAIL = "It is a picture of the screen as text. The peer cannot type, resize or scroll back.";
const LIMIT_TEXT = `Know the limit${LEAD} ${UNPROTECTED}${NOT_SHOWN}${DETAIL}`;
const UNAVAILABLE = "This screen can't be shown right now.";
const UNAVAILABLE_LATER = "It will appear when the screen can be read again.";
const UNAVAILABLE_TEXT = `${UNAVAILABLE}${NOT_SHOWN}${UNAVAILABLE_LATER}`;
const NOT_A_FRAME_TEXT = `${UNAVAILABLE}What arrived is not a screen this page can draw.${UNAVAILABLE_LATER}`;

/** What a CSS token resolves to as a colour, in the page's current theme. */
const token = (page: Page, name: string, prop: "color" | "borderLeftColor" = "color") =>
  page.evaluate(
    ([n, p]) => {
      const probe = document.createElement("span");
      probe.style.setProperty(p === "color" ? "color" : "border-left", p === "color" ? `var(${n})` : `1px solid var(${n})`);
      document.body.append(probe);
      const value = getComputedStyle(probe)[p as "color"];
      probe.remove();
      return value;
    },
    [name, prop] as const,
  );

async function openTerminalDialog(page: Page): Promise<{ dialog: Locator; opener: Locator }> {
  await page.goto(TERM_URL);
  await expect(page.locator(".xterm-rows")).toBeVisible();
  const opener = await clickHeadAction(page, SHARE_TERMINAL);
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await animationsSettled(page);
  return { dialog, opener };
}

/** The receiving instance: the API rows of `share-peer.spec.ts` plus two shared terminals. */
function withTerminals(f: FakeShare) {
  f.remote.push(
    f.screenRow(GPU, "gpu-box", GRANT_T, TERM_TITLE, true),
    f.screenRow(GPU, "gpu-box", GRANT_T2, "Nightly build watch", true, 30),
  );
}

async function openScreen(page: Page, grant = GRANT_T) {
  await page.goto(remotePath(GPU, grant));
  const pane = page.getByTestId("remote-pane");
  await expect(pane).toBeVisible();
  return pane;
}

const frameReads = (f: FakeShare, grant = GRANT_T) => f.reads.filter((r) => r.kind === "frame" && r.grant === grant);
const have = (search: string) => new URLSearchParams(search).get("have");
const lines = (pane: Locator) => pane.getByTestId("remote-screen-text").locator("> div").allTextContents();

// =================================================================================================
// The owner's side
// =================================================================================================

test("the terminal pane's Share offers Peer…, in the menu's own form, and Public… switched off (#2173)", async ({ page }) => {
  await setupTerminal(page);
  await page.goto(TERM_URL);
  await expect(page.locator(".xterm-rows")).toBeVisible();
  if (isMobile(page)) {
    // ≤800px: the one Actions menu, an anchored popover (docs/design.md §5), with the Share
    // group: its label once, then Public… (off, with the reason) and Peer….
    await page.getByTestId("head-actions-menu").click();
    const menu = page.getByRole("menu", { name: ACTIONS_MENU });
    await expect(menu).toBeVisible();
    const item = menu.getByRole("menuitem", { name: SHARE_TERMINAL });
    await expect(item).toHaveText("Peer…");
    await expect(menu.locator("[data-menu-group]")).toHaveText(["Session", "Pane", "Share"]);
    // The last entries: nothing that was in the menu moved for them.
    const names = await menu.getByRole("menuitem").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
    expect(names.slice(-2)).toEqual([SHARE_PUBLIC, SHARE_TERMINAL]);
    const off = menu.getByRole("menuitem", { name: SHARE_PUBLIC });
    await expect(off).toBeDisabled();
    await expect(off).toContainText(SHARE_PUBLIC_UNAVAILABLE);
    // A popover under its trigger, not a sheet: the terminal stays visible around it.
    const box = (await menu.boundingBox())!;
    expect(box.width).toBeLessThan(page.viewportSize()!.width);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect((await item.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    await page.keyboard.press("Escape");
  }
  const opener = await clickHeadAction(page, SHARE_TERMINAL);
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("data-kind", "terminal");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  // A terminal has no public link (#1404): Public… is never a control of its own on the bar.
  await expect(page.getByRole("button", { name: /share this session publicly/i })).toHaveCount(0);
});

test("the dialog for a terminal: the limit first on the attention edge, only View, and it shares", async ({
  page,
}) => {
  const fake = await setupTerminal(page);
  const { dialog } = await openTerminalDialog(page);

  // The session line says what is shared.
  await expect(dialog.getByText(TERM_TITLE)).toBeVisible();
  await expect(dialog.locator("p").filter({ hasText: TERM_TITLE })).toContainText("Terminal");

  // The limit: the first thing in the body, above the peer picker, word for word.
  const body = dialog.getByTestId("share-peer-body");
  const limit = dialog.getByTestId("share-peer-limit");
  await expect(limit).toBeVisible();
  expect(await body.evaluate((el) => el.firstElementChild?.getAttribute("data-testid"))).toBe("share-peer-limit");
  await expect(limit).toHaveText(LIMIT_TEXT);
  await expect(limit.getByTestId("share-peer-not-shown")).toHaveText(NOT_SHOWN);
  const firstPeer = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio").first();
  const limitBox = (await limit.boundingBox())!;
  expect((await firstPeer.boundingBox())!.y).toBeGreaterThanOrEqual(limitBox.y + limitBox.height);
  // The unprotected case is the bold sentence, and only that one.
  const bold = limit.locator("b");
  await expect(bold).toHaveCount(1);
  await expect(bold).toHaveText(UNPROTECTED);
  const weights = await limit.evaluate((el) => [
    Number(getComputedStyle(el).fontWeight),
    Number(getComputedStyle(el.querySelector("b")!).fontWeight),
  ]);
  expect(weights[1]).toBeGreaterThan(weights[0]);
  expect(weights[1]).toBeLessThanOrEqual(600); // the app's weights, not the mockup's 700
  // The amber attention edge AND a word: never red, never colour alone.
  expect(await limit.evaluate((el) => getComputedStyle(el).borderLeftColor)).toBe(
    await token(page, "--status-degraded", "borderLeftColor"),
  );
  expect(await limit.evaluate((el) => getComputedStyle(el).borderLeftColor)).not.toBe(
    await token(page, "--status-down", "borderLeftColor"),
  );
  await expect(limit).toContainText("Know the limit");
  expect(await limit.evaluate((el) => getComputedStyle(el).boxShadow)).toBe("none");
  // The API session's disclosure is not this dialog's.
  await expect(dialog.getByTestId("share-peer-disclosure")).toHaveCount(0);

  // Access: View alone. Suggest and Send are not offered, not even as "later".
  const access = dialog.getByRole("radiogroup", { name: "Access" }).getByRole("radio");
  await expect(access).toHaveCount(1);
  await expect(access.first()).toHaveText("ViewWatch the screen");
  await expect(access.first()).toHaveAttribute("aria-checked", "true");
  await expect(dialog.getByText("Suggest")).toHaveCount(0);
  await expect(dialog.getByText("Send", { exact: true })).toHaveCount(0);
  await expect(dialog.getByText("later")).toHaveCount(0);
  await expect(dialog.getByTestId("share-peer-terminal-only")).toHaveText("A terminal can only be watched.");

  // The peer picker and the expiry are the API dialog's.
  const peers = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio");
  await expect(peers).toHaveCount(3);
  await expect(peers.nth(0)).toContainText("gpu-box");
  await expect(peers.nth(0)).toContainText("connected");
  await expect(dialog.getByTestId("share-peer-hint")).toContainText(`Ends ${date(NOW() + 7 * DAY)}.`);
  await noHorizontalOverflow(page);

  // Share: the grant is made with the TERMINAL session's key, view only.
  await dialog.getByTestId("share-peer-go").click();
  await expect(dialog.getByTestId("share-peer-done")).toHaveText(
    `Shared with gpu-box (View) until ${date(NOW() + 7 * DAY)}.`,
  );
  const post = fake.writes.find((w) => w.method === "POST" && w.path === "/grants")!;
  expect(post.body).toEqual({ session_key: TERM_KEY, peer_id: GPU, expires_in: 7 * DAY, level: "view" });
  expect(post.csrf).toBe(CSRF);
  expect(fake.grants[0].kind).toBe("terminal");

  // It is listed, and Revoke ends it from the same dialog (the one red control).
  const row = dialog.getByTestId("share-peer-grant");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("gpu-box");
  await expect(row).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  await row.getByRole("button", { name: "Revoke gpu-box" }).click();
  await expect(row).toHaveCount(0);
  expect(fake.writes.at(-1)!.path).toMatch(/^\/grants\/[^/]+\/revoke$/);
  expect(fake.grants).toEqual([]);
});

test("a refusal from the server is shown in the terminal dialog with its words", async ({ page }) => {
  await setupTerminal(page, (f) => {
    f.refuse["POST /grants"] = { status: 409, detail: "this session is archived and cannot be shared" };
  });
  const { dialog } = await openTerminalDialog(page);
  await dialog.getByTestId("share-peer-go").click();
  await expect(dialog.getByTestId("share-peer-error")).toHaveText("this session is archived and cannot be shared");
  // The limit is still the first thing there.
  await expect(dialog.getByTestId("share-peer-limit")).toBeVisible();
});

test("on a phone the sheet opens with the limit on screen, before anything is scrolled", async ({ page }) => {
  test.skip(!isMobile(page), "the sheet is the ≤800px form");
  // The worst case for the opening position: existing shares make the body long.
  await setupTerminal(page, (f) => {
    f.grants.push(
      f.grant(GPU, { session_key: TERM_KEY, kind: "terminal" }),
      f.grant(LAPTOP, { session_key: TERM_KEY, kind: "terminal", expires_at: NOW() + 30 * DAY }),
    );
  });
  const { dialog } = await openTerminalDialog(page);
  const body = dialog.getByTestId("share-peer-body");
  const limit = dialog.getByTestId("share-peer-limit");
  // Nothing was scrolled: this is where the sheet opens.
  expect(await body.evaluate((el) => el.scrollTop)).toBe(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  // The body does scroll (so "on screen" is not true of everything in it)...
  expect(await body.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeGreaterThan(40);
  // ...and the whole limit box is inside the body's visible part and inside the viewport.
  const view = page.viewportSize()!;
  const box = (await limit.boundingBox())!;
  const bodyBox = (await body.boundingBox())!;
  expect(box.y).toBeGreaterThanOrEqual(bodyBox.y - 0.5);
  expect(box.y + box.height).toBeLessThanOrEqual(bodyBox.y + bodyBox.height + 0.5);
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(view.height);
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(view.width + 0.5);
  await expect(limit).toBeInViewport({ ratio: 1 });
  await expect(limit.locator("b")).toBeInViewport({ ratio: 1 });
  await expect(limit.getByTestId("share-peer-not-shown")).toBeInViewport({ ratio: 1 });
  // It is what the operator reads first: the peer picker starts under it.
  const firstPeer = dialog.getByRole("radiogroup", { name: "Peer" }).getByRole("radio").first();
  expect((await firstPeer.boundingBox())!.y).toBeGreaterThanOrEqual(box.y + box.height);
  // The sheet keeps its actions at the floor, and every control is a 44 px target.
  await expect(dialog.getByTestId("share-peer-go")).toBeInViewport({ ratio: 1 });
  await noHorizontalOverflow(page);
  await controlsAtLeast44(dialog);
  // The existing shares are reachable under it, each with Revoke.
  const revoke = dialog.getByRole("button", { name: "Revoke laptop" });
  await revoke.scrollIntoViewIfNeeded();
  await expect(revoke).toBeInViewport({ ratio: 1 });
  expect(await body.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
});

// =================================================================================================
// The receiving side: rows, the map, the pane choice
// =================================================================================================

test("a shared terminal's row carries SCREEN; it opens the screen pane, an API row the conversation", async ({
  page,
}) => {
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.remote.push(f.screenRow(LAPTOP, "laptop", "n2Oc4fGhIjKlMnOpQrStUv", "Field terminal", false));
  });
  await page.goto(URL_PATH);
  const list = await showShared(page);
  const rows = list.getByTestId("remote-row");
  await expect(rows).toHaveCount(6);

  // The terminal row: badge · SCREEN · "until <date>".
  const term = rows.filter({ hasText: TERM_TITLE });
  await expect(term).toHaveAttribute("data-kind", "terminal");
  await expect(term.getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(term.getByTestId("screen-tag")).toHaveText("Screen");
  await expect(term.getByTestId("remote-row-meta")).toHaveText(`until ${date(NOW() + 7 * DAY)}`);
  const order = await term.evaluate((el) =>
    [...el.querySelectorAll("[data-testid]")].map((n) => n.getAttribute("data-testid")),
  );
  expect(order).toEqual(["peer-badge", "screen-tag", "remote-row-meta"]);
  // The tag is neutral ink: a kind is not a state.
  expect(await term.getByTestId("screen-tag").evaluate((el) => getComputedStyle(el).color)).toBe(
    await token(page, "--text-2"),
  );
  // An offline terminal: badge · SCREEN · "offline".
  const off = rows.filter({ hasText: "Field terminal" });
  await expect(off.getByTestId("screen-tag")).toBeVisible();
  await expect(off.getByTestId("remote-row-meta")).toHaveText("offline");
  // An API row is what it was: no tag.
  const apiRow = rows.filter({ hasText: "Tune the render queue" });
  await expect(apiRow).toHaveAttribute("data-kind", "api");
  await expect(apiRow.getByTestId("screen-tag")).toHaveCount(0);
  await expect(apiRow.getByTestId("remote-row-meta")).toHaveText(`view only · until ${date(NOW() + 7 * DAY)}`);
  if (isMobile(page)) {
    for (const row of [term, off, apiRow])
      expect((await row.locator("a").boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }

  // The terminal row opens the screen pane: a frame read, never the session's verbs.
  await term.locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_T}$`));
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  await expect(pane.getByTestId("remote-transcript")).toHaveCount(0);
  await expect.poll(() => frameReads(fake).length).toBeGreaterThanOrEqual(1);
  expect(fake.reads.filter((r) => r.grant === GRANT_T && r.kind !== "frame")).toEqual([]);
  expect(have(frameReads(fake)[0].search)).toBeNull();
  expect(new URLSearchParams(frameReads(fake)[0].search).get("wait")).toBe("3");

  // An API row still opens the conversation pane, and asks for no frame.
  await showShared(page);
  await apiRow.locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_A}$`));
  await expect(pane.getByTestId("remote-transcript")).toBeVisible();
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  await expect(pane.getByTestId("screen-tag")).toHaveCount(0);
  await expect(pane.getByTestId("remote-banner")).toContainText("view only");
  expect(fake.reads.filter((r) => r.grant === GRANT_A && r.kind === "frame")).toEqual([]);
});

test("the map draws a shared terminal with the SCREEN tag, and it opens the screen pane", async ({ page }) => {
  await setup(page, withTerminals);
  await page.goto(MAP_PATH);
  const nodes = page.getByTestId("remote-node");
  await expect(nodes).toHaveCount(5);
  const node = nodes.filter({ hasText: TERM_TITLE });
  await expect(node).toHaveAttribute("data-kind", "terminal");
  await expect(node.getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(node.getByTestId("screen-tag")).toHaveText("Screen");
  await expect(node).not.toContainText(/view only/i);
  await expect(node).toContainText(`until ${date(NOW() + 7 * DAY)}`);
  await expect(node.locator("button")).toHaveCount(0);
  // An API node is what it was.
  const apiNode = nodes.filter({ hasText: "Tune the render queue" });
  await expect(apiNode.getByTestId("screen-tag")).toHaveCount(0);
  await expect(apiNode).toContainText(/view only/i);
  // A click opens the pane's own route, never a map window.
  await node.click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_T}$`));
  await expect(page.getByTestId("remote-pane").getByTestId("remote-screen")).toBeVisible();
});

// =================================================================================================
// The screen pane
// =================================================================================================

test("the screen pane: head, banner, the frame with its columns intact and a masked run as █", async ({
  page,
}) => {
  const ruler = `+${"-".repeat(98)}+`;
  const cells = `|${"0123456789".repeat(9)}${MASK.repeat(8)}|`; // 100 characters, 8 of them masked
  const shownLines = [ruler, cells, ...SCREEN_LINES, "", "    trailing spaces kept    "];
  await setup(page, (f) => {
    withTerminals(f);
    f.onRead = () => ({ json: frame({ lines: shownLines }) });
  });
  const pane = await openScreen(page);

  // Head: title, SCREEN tag, the peer's badge. Banner: who, "screen only", until when, the state.
  await expect(pane.getByTestId("remote-title")).toHaveText(TERM_TITLE);
  const head = pane.locator("header");
  await expect(head.getByTestId("screen-tag")).toHaveText("Screen");
  await expect(head.getByTestId("peer-badge")).toHaveText("gpu-box");
  await expect(pane.getByTestId("remote-banner")).toContainText(
    `Shared by gpu-box · screen only · until ${date(NOW() + 7 * DAY)}`,
  );
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
  expect(await pane.getByTestId("remote-state").locator("span").last().evaluate((el) => getComputedStyle(el).color)).toBe(
    await token(page, "--ok-text"),
  );
  await expect(pane.getByTestId("remote-screen-only")).toHaveText("Screen only. No typing, no scrollback.");
  await expect(pane).toHaveAttribute("aria-label", `${TERM_TITLE}, shared by gpu-box, screen only`);
  expect(await pane.evaluate((el) => getComputedStyle(el).borderTopStyle)).toBe("dashed");
  await expect(pane.getByTestId("remote-screen-dims")).toHaveText("100 × 30");
  await expect(pane.getByTestId("remote-screen-marker")).toHaveCount(0);

  // The frame: exactly `rows` lines, each the text it came as (leading and trailing blanks too).
  const text = pane.getByTestId("remote-screen-text");
  const drawn = await lines(pane);
  expect(drawn).toHaveLength(30);
  expect(drawn.slice(0, shownLines.length)).toEqual(shownLines);
  expect(drawn.slice(shownLines.length).every((l) => l === "")).toBe(true);
  // Every row is text nodes. The only elements are the one-column boxes of characters the
  // monospace face has no glyph for (here the masked ones), each holding ONE character as text.
  const shape = await text.evaluate((el) => {
    const rows = [...el.children];
    const cells = rows.flatMap((row) => [...row.children]);
    return {
      rows: rows.every((row) => row.tagName === "DIV"),
      leaves: rows.every((row) =>
        [...row.childNodes].every(
          (n) => n.nodeType === Node.TEXT_NODE || (n as Element).tagName === "SPAN",
        ),
      ),
      cells: cells.every(
        (c) =>
          c.childNodes.length === 1 &&
          c.firstChild!.nodeType === Node.TEXT_NODE &&
          [...c.textContent!].length === 1 &&
          c.getAttributeNames().join() === "class",
      ),
      celled: [...new Set(cells.map((c) => c.textContent))],
      plain: rows[2].childNodes.length === 1 && rows[2].firstChild!.nodeType === Node.TEXT_NODE,
    };
  });
  expect(shape).toEqual({ rows: true, leaves: true, cells: true, celled: [MASK], plain: true });
  // A masked run is shown as it is, and the pane offers nothing to reveal or copy it.
  await expect(text).toContainText(`API_TOKEN=${MASK.repeat(8)}`);
  // Its one control is Leave in the banner (#2201): it gives the share up and touches no screen.
  const controls = pane.locator("button, a, input, textarea, select, [contenteditable], [role=button]");
  await expect(controls).toHaveCount(1);
  await expect(controls).toHaveText("Leave");
  await expect(pane.getByTestId("remote-banner").getByTestId("share-leave")).toHaveCount(1);
  await expect(pane.locator("[title*=reveal i], [title*=copy i], [aria-label*=reveal i], [aria-label*=copy i]")).toHaveCount(0);

  // The block is exactly `cols` characters wide, monospace, and the columns line up.
  const m = await text.evaluate((el) => {
    const cs = getComputedStyle(el);
    const probe = document.createElement("span");
    probe.style.font = cs.font;
    probe.style.letterSpacing = cs.letterSpacing;
    probe.style.whiteSpace = "pre";
    probe.style.position = "absolute";
    probe.textContent = "0".repeat(100);
    el.append(probe);
    const hundred = probe.getBoundingClientRect().width;
    probe.remove();
    const width = (row: Element) => {
      const range = document.createRange();
      range.selectNodeContents(row);
      return range.getBoundingClientRect().width;
    };
    const rows = [...el.children];
    return {
      hundred,
      content: el.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
      ruler: width(rows[0]),
      cells: width(rows[1]),
      heights: [...new Set(rows.map((r) => Math.round(r.getBoundingClientRect().height * 10) / 10))],
      family: cs.fontFamily,
      wrap: getComputedStyle(rows[0]).whiteSpace,
    };
  });
  expect(m.family.toLowerCase()).toContain("mono");
  expect(m.wrap).toBe("pre");
  expect(Math.abs(m.content - m.hundred)).toBeLessThan(0.75); // the block: 100 columns
  expect(Math.abs(m.ruler - m.hundred)).toBeLessThan(0.75); // a 100-character line fills it exactly
  expect(Math.abs(m.cells - m.hundred)).toBeLessThan(0.75); // ...and so does one with a masked run
  expect(m.heights).toHaveLength(1); // every row the same height, the empty ones too
  await noHorizontalOverflow(page);
});

test("a wide frame scrolls inside its own block at 390 px; the page does not overflow", async ({ page }) => {
  test.skip(!isMobile(page), "the phone layout");
  await setup(page, (f) => {
    withTerminals(f);
    f.onRead = () => ({ json: frame({ cols: 160, lines: [`${"=".repeat(159)}!`, ...SCREEN_LINES] }) });
  });
  const pane = await openScreen(page);
  const screen = pane.getByTestId("remote-screen");
  await expect(screen).toBeVisible();
  expect(page.viewportSize()!.width).toBeLessThanOrEqual(430);
  await noHorizontalOverflow(page);
  // The frame is wider than the pane and scrolls sideways INSIDE the block.
  const sizes = await screen.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  expect(sizes.scroll).toBeGreaterThan(sizes.client + 200);
  expect(sizes.client).toBeLessThanOrEqual(page.viewportSize()!.width);
  await screen.evaluate((el) => el.scrollTo({ left: el.scrollWidth }));
  expect(await screen.evaluate((el) => el.scrollLeft)).toBeGreaterThan(200);
  // The last column is reachable, and the page itself never moved.
  const end = await screen.evaluate((el) => {
    const row = el.querySelector("[data-testid=remote-screen-text] > div")!;
    const range = document.createRange();
    range.setStart(row.firstChild!, 159);
    range.setEnd(row.firstChild!, 160);
    const r = range.getBoundingClientRect();
    return { left: r.left, right: r.right };
  });
  expect(end.left).toBeGreaterThanOrEqual(0);
  expect(end.right).toBeLessThanOrEqual(page.viewportSize()!.width + 0.5);
  expect(await page.evaluate(() => window.scrollX)).toBe(0);
  await noHorizontalOverflow(page);
  // The head, the banner and the foot do not scroll away with it.
  for (const id of ["remote-title", "remote-banner", "remote-screen-only"]) {
    const box = (await pane.getByTestId(id).boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize()!.width + 0.5);
  }
  // Nothing to tap in the pane but Leave in its banner (#2201), a 44 px target.
  await expect(pane.locator("button, a")).toHaveCount(1);
  await controlsAtLeast44(pane.getByTestId("remote-banner"));
});

// =================================================================================================
// The states, each from what the server answered
// =================================================================================================

test("an unsettled frame is shown at full strength with updating…; `unchanged` clears it; the next frame replaces it", async ({
  page,
}) => {
  const next = gate();
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url, n) => {
      const h = url.searchParams.get("have");
      if (n === 1) return { json: frame({ settled: false }) }; // taken while the screen was changing
      if (n === 2) return unchanged(String(h)); // the owner confirms: that IS the settled screen
      if (n === 3) return { hold: next.promise, json: frame({ fingerprint: FP_B, settled: false, lines: ["$ make deploy", "building…"] }) };
      if (n === 4) return { json: frame({ fingerprint: "c".repeat(32), lines: ["$ make deploy", "done."] }) };
      return unchanged(String(h));
    };
  });
  const pane = await openScreen(page);
  const screen = pane.getByTestId("remote-screen");
  const marker = pane.getByTestId("remote-screen-marker");
  const ink = await token(page, "--text-1");

  // 1. The unsettled frame: there, at full strength (it is fresh), marked.
  await expect(screen).toBeVisible();
  await expect(marker).toHaveText("updating…");
  await expect(screen).not.toHaveAttribute("data-dimmed", /.*/);
  expect(await pane.getByTestId("remote-screen-text").evaluate((el) => getComputedStyle(el).color)).toBe(ink);
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
  // The marker is grey: updating is not an alarm.
  expect(await marker.evaluate((el) => getComputedStyle(el).color)).toBe(await token(page, "--text-2"));

  // 2. `unchanged` for that very fingerprint: the same frame, and the marker is gone.
  await expect(marker).toHaveCount(0);
  await expect(screen).toBeVisible();
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
  expect(have(frameReads(fake)[1].search)).toBe(FP_A); // the fingerprint of the frame on screen
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");

  // 3. Another unsettled frame replaces it, marked again...
  await expect.poll(() => frameReads(fake).length).toBe(3);
  next.open();
  await expect(marker).toHaveText("updating…");
  await expect.poll(async () => (await lines(pane)).slice(0, 2)).toEqual(["$ make deploy", "building…"]);
  await expect(screen).not.toHaveAttribute("data-dimmed", /.*/);
  // 4. ...and is replaced by the next frame, settled: no marker.
  await expect.poll(async () => (await lines(pane)).slice(0, 2)).toEqual(["$ make deploy", "done."]);
  await expect(marker).toHaveCount(0);
  expect(have(frameReads(fake)[3].search)).toBe(FP_B);
  await expect.poll(() => frameReads(fake).length).toBeGreaterThanOrEqual(5);
  expect(have(frameReads(fake)[4].search)).toBe("c".repeat(32));
});

test("`settling`: updating… alone with no frame yet; the last frame dimmed once there is one", async ({ page }) => {
  const first = gate();
  const busy = gate();
  const more = gate();
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url, n) => {
      const h = url.searchParams.get("have");
      if (n === 1) return SETTLING;
      if (n === 2) return { hold: first.promise, json: frame() };
      if (n === 3) return { hold: busy.promise, ...SETTLING };
      if (n === 4) return { hold: more.promise, json: frame({ fingerprint: FP_B, lines: ["$ ls", "deploy.log"] }) };
      return unchanged(String(h));
    };
  });
  const pane = await openScreen(page);
  const screen = pane.getByTestId("remote-screen");
  const marker = pane.getByTestId("remote-screen-marker");

  // No frame yet: the marker alone, and nothing that looks like a screen.
  await expect(marker).toHaveText("updating…");
  await expect(screen).toHaveCount(0);
  await expect(pane.getByTestId("remote-screen-dims")).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
  await expect.poll(() => frameReads(fake).length).toBe(2);
  expect(have(frameReads(fake)[1].search)).toBeNull(); // nothing on screen, so no `have`

  // The frame arrives...
  first.open();
  await expect(screen).toBeVisible();
  await expect(marker).toHaveCount(0);
  const ink = await pane.getByTestId("remote-screen-text").evaluate((el) => getComputedStyle(el).color);
  expect(ink).toBe(await token(page, "--text-1"));
  await expect(screen).not.toHaveAttribute("data-dimmed", /.*/);
  await expect(screen).toHaveAttribute("aria-label", "The shared terminal screen, as text");
  // ...and the next answer is `settling`: the same frame stays, dimmed, under the marker.
  await expect.poll(() => frameReads(fake).length).toBe(3);
  busy.open();
  await expect(screen).toHaveAttribute("data-dimmed", "true");
  await expect(marker).toHaveText("updating…");
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
  expect(await pane.getByTestId("remote-screen-text").evaluate((el) => getComputedStyle(el).color)).toBe(
    await token(page, "--text-2"),
  );
  await expect(screen).toHaveAttribute("aria-label", "The last screen, while a new one is read");
  await expect(pane.getByTestId("remote-state")).toHaveText("connected"); // grey marker, green peer
  // The new frame replaces it at full strength.
  await expect.poll(() => frameReads(fake).length).toBe(4);
  expect(have(frameReads(fake)[3].search)).toBe(FP_A); // still the frame on screen
  more.open();
  await expect.poll(async () => (await lines(pane)).slice(0, 2)).toEqual(["$ ls", "deploy.log"]);
  await expect(screen).not.toHaveAttribute("data-dimmed", /.*/);
  await expect(marker).toHaveCount(0);
  expect(await pane.getByTestId("remote-screen-text").evaluate((el) => getComputedStyle(el).color)).toBe(ink);
});

test("the dimmed frame is dropped after 30 s; the next request carries no `have` and the pane fills again", async ({
  page,
}) => {
  const late = gate();
  const refill = gate();
  // The owner. With nothing on the asker's screen it sends the current frame. Asked with the
  // fingerprint of that frame it says the screen is still changing (`settling`) while it is
  // busy, and `unchanged` once it is calm. So a pane that dropped its frame but still sent the
  // old fingerprint would be told `unchanged`, and stay empty for good.
  const owner = { calm: false, busyAsked: 0, bare: 0 };
  const aborted: string[] = [];
  page.on("requestfailed", (r) => {
    if (/\/frame$/.test(new URL(r.url()).pathname)) aborted.push(new URL(r.url()).search);
  });
  await page.clock.install();
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      const h = url.searchParams.get("have");
      if (h === null) {
        owner.bare += 1;
        return owner.bare === 1 ? { json: frame() } : { hold: refill.promise, json: frame() };
      }
      if (owner.calm) return unchanged(h);
      owner.busyAsked += 1;
      // The first `settling` is answered; the second read is a long poll still in flight when
      // the bound fires.
      return owner.busyAsked === 1 ? SETTLING : { hold: late.promise, label: "late", ...unchanged(h) };
    };
  });
  const pane = await openScreen(page);
  const screen = pane.getByTestId("remote-screen");
  const marker = pane.getByTestId("remote-screen-marker");

  // A frame, then `settling`: dimmed, marked. The bound's clock starts here.
  await expect(screen).toHaveAttribute("data-dimmed", "true");
  await expect(marker).toHaveText("updating…");
  await expect.poll(() => frameReads(fake).length).toBe(3);
  expect(frameReads(fake).map((r) => have(r.search))).toEqual([null, FP_A, FP_A]);
  // From here the owner would answer `unchanged` to the old fingerprint.
  owner.calm = true;

  // Well inside the bound the dimmed frame is still there.
  await page.clock.fastForward(20_000);
  await painted(page);
  await expect(screen).toHaveAttribute("data-dimmed", "true");
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
  expect(frameReads(fake)).toHaveLength(3);

  // Past it: the frame is dropped, and updating… is shown alone.
  await page.clock.fastForward(11_000);
  await expect(screen).toHaveCount(0);
  await expect(marker).toHaveText("updating…");
  await expect(pane.getByText(SCREEN_LINES[0])).toHaveCount(0);
  await expect(pane.getByTestId("remote-screen-dims")).toHaveCount(0);
  // The read that was in flight was given up, and the next one carries NO `have`.
  await expect.poll(() => frameReads(fake).length).toBe(4);
  expect(have(frameReads(fake)[3].search)).toBeNull();
  expect(frameReads(fake)[3].search).not.toContain("have");
  expect(aborted.some((s) => have(s) === FP_A)).toBe(true);

  // The pane fills again from the answer: an empty pane cannot stay empty.
  refill.open();
  await expect(screen).toBeVisible();
  await expect(screen).not.toHaveAttribute("data-dimmed", /.*/);
  await expect(marker).toHaveCount(0);
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);

  // The late answer to the dropped read is sent now. It changes nothing.
  late.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  await expect(screen).toBeVisible();
  await expect(marker).toHaveCount(0);
});

test("503 unavailable: the frame is dropped, the state says so, and the screen comes back", async ({ page }) => {
  const state = { mode: "live" as "live" | "unavailable" };
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      if (state.mode === "unavailable") return SCREEN_UNAVAILABLE;
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const pane = await openScreen(page);
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  state.mode = "unavailable";
  const empty = pane.getByTestId("remote-screen-unavailable");
  await expect(empty).toHaveText(UNAVAILABLE_TEXT);
  await expect(empty).toHaveAttribute("data-reason", "screen");
  await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  await expect(pane.getByText(SCREEN_LINES[0])).toHaveCount(0);
  await expect(pane.getByTestId("remote-screen-marker")).toHaveCount(0);
  // The peer IS connected: green stays; the state itself is neutral ink, not an alarm.
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
  expect(await empty.locator("p").first().evaluate((el) => getComputedStyle(el).color)).toBe(
    await token(page, "--text-1"),
  );
  await expect(pane.getByTestId("remote-banner")).toContainText("screen only");
  await expect(pane.getByTestId("remote-screen-only")).toBeVisible();
  // It asks again, with no `have` (nothing is on screen), and fills when the screen can be read.
  const asked = frameReads(fake).length;
  await expect.poll(() => frameReads(fake).length, { timeout: 10_000 }).toBeGreaterThan(asked);
  expect(have(frameReads(fake).at(-1)!.search)).toBeNull();
  state.mode = "live";
  await expect(pane.getByTestId("remote-screen")).toBeVisible({ timeout: 10_000 });
  await expect(empty).toHaveCount(0);
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
});

// #2055 review (P1), the same class: a peer's numbers are untrusted. A frame whose `rows`, `cols`
// or lines fail the browser's check is "not viewable": nothing is sized, padded or looped by it,
// nothing throws, the app stays up, and the next good frame is drawn.
test("a frame with hostile numbers is not viewable, throws nothing, and the next good frame is drawn", async ({
  page,
}) => {
  // Each bad frame is followed by the 3 s pause before the next read.
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const hostile: Record<string, unknown>[] = [
    { rows: 1e300 },
    { cols: 1e300 },
    { rows: 10_000_000, lines: [] },
    { cols: -80 },
    { lines: ["x".repeat(101)] }, // cols is 100
    { lines: [{ length: 1e9 }] },
  ];
  const state = { bad: 0 as number | null };
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      if (state.bad !== null) return { json: { ...frame(), ...hostile[state.bad] } };
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const pane = await openScreen(page);
  const empty = pane.getByTestId("remote-screen-unavailable");
  for (let i = 0; i < hostile.length; i++) {
    state.bad = i;
    const asked = frameReads(fake).length;
    await expect.poll(() => frameReads(fake).length, { timeout: 15_000 }).toBeGreaterThan(asked);
    await expect(empty).toHaveText(NOT_A_FRAME_TEXT);
    await expect(empty).toHaveAttribute("data-reason", "frame");
    await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  }
  await noHorizontalOverflow(page);
  state.bad = null;
  await expect(pane.getByTestId("remote-screen")).toBeVisible({ timeout: 15_000 });
  expect((await lines(pane))[0]).toBe(SCREEN_LINES[0]);
  expect(errors).toEqual([]);
});

test("503 peer_offline: the frame is dropped and the pane says the peer is not connected", async ({ page }) => {
  const state = { mode: "live" as "live" | "offline" };
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      if (state.mode === "offline") return OFFLINE;
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const pane = await openScreen(page);
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  state.mode = "offline";
  await expect(pane.getByTestId("remote-empty")).toHaveText(
    "gpu-box is not connected. Showing nothing until it is back.",
  );
  await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  await expect(pane.getByText(SCREEN_LINES[0])).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("offline");
  // Grey, not green and not red.
  expect(await pane.getByTestId("remote-state").locator("span").last().evaluate((el) => getComputedStyle(el).color)).toBe(
    await token(page, "--text-2"),
  );
  await expect(pane.locator("header").getByTestId("screen-tag")).toBeVisible();
  const asked = frameReads(fake).length;
  await expect.poll(() => frameReads(fake).length, { timeout: 10_000 }).toBeGreaterThan(asked);
  expect(have(frameReads(fake).at(-1)!.search)).toBeNull();
  state.mode = "live";
  await expect(pane.getByTestId("remote-screen")).toBeVisible({ timeout: 10_000 });
  await expect(pane.getByTestId("remote-state")).toHaveText("connected");
});

test("404: the share is no longer available, the frame is dropped, and the row is gone on the next list read", async ({
  page,
}) => {
  const state = { gone: false };
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      if (state.gone) return GONE;
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const pane = await openScreen(page);
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  // The owner revokes: its list no longer has the grant, and the read says 404.
  fake.remote = fake.remote.filter((r) => r.grant_id !== GRANT_T);
  state.gone = true;
  await expect(pane.getByTestId("remote-empty")).toHaveText("This share is no longer available.");
  await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  await expect(pane.getByText(SCREEN_LINES[0])).toHaveCount(0);
  await expect(pane.getByTestId("remote-state")).toHaveText("unavailable");
  await expect(pane.getByTestId("remote-screen-only")).toHaveCount(0);
  // Still the screen pane (it did not turn into the conversation pane when the row left)...
  await expect(pane.locator("header").getByTestId("screen-tag")).toBeVisible();
  await expect(pane.getByTestId("remote-title")).toHaveText(TERM_TITLE);
  await expect(pane.getByTestId("remote-transcript")).toHaveCount(0);
  // ...the row is gone from the list...
  const list = await showShared(page);
  await expect(list.getByTestId("remote-row").filter({ hasText: TERM_TITLE })).toHaveCount(0);
  // ...and nothing is asked again: final.
  const asked = fake.reads.length;
  await page.waitForTimeout(4_000);
  expect(fake.reads.length).toBe(asked);
  expect(fake.reads.filter((r) => r.grant === GRANT_T && r.kind !== "frame")).toEqual([]);
});

// =================================================================================================
// The fence
// =================================================================================================

async function screenWithHeldFrame(page: Page) {
  const held = gate();
  const aborted: string[] = [];
  page.on("requestfailed", (r) => {
    if (new RegExp(`/${GRANT_T}/frame$`).test(new URL(r.url()).pathname)) aborted.push(r.url());
  });
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (kind, grant, url, n) => {
      const h = url.searchParams.get("have");
      if (kind !== "frame") return kind === "snapshot" ? { json: snapshot(RENDER_TURNS) } : QUIET;
      if (grant === GRANT_T2)
        return h ? unchanged(h) : { json: frame({ fingerprint: "d".repeat(32), cols: 40, rows: 4, lines: ["$ tail -f build.log", "ok"] }) };
      if (n === 1) return { json: frame() };
      if (n === 2)
        return { hold: held.promise, label: "late", json: frame({ fingerprint: FP_B, lines: ["LATE SCREEN LINE"] }) };
      return unchanged(String(h));
    };
  });
  await page.goto(URL_PATH);
  const list = await showShared(page);
  await list.getByTestId("remote-row").filter({ hasText: TERM_TITLE }).locator("a").click();
  const pane = page.getByTestId("remote-pane");
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  // The second frame read is on its way and held.
  await expect.poll(() => frameReads(fake).length).toBe(2);
  return { fake, pane, held, aborted };
}

test("a slow frame that arrives after navigating away is discarded", async ({ page }) => {
  const { fake, held, aborted } = await screenWithHeldFrame(page);
  // Leave for a local session, inside the app (no reload: the same document keeps running).
  const list = await showLocal(page);
  await list.locator(`a[href="${URL_PATH}"]`).click();
  await expect(page.getByTestId("structured-pane")).toBeVisible();
  await expect(page.getByTestId("remote-pane")).toHaveCount(0);
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  // The pane's read was given up when it left, and nothing of the peer's screen is on the page.
  expect(aborted.length).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("LATE SCREEN LINE")).toHaveCount(0);
  await expect(page.getByText(SCREEN_LINES[0])).toHaveCount(0);
  // ...and nothing asks that share anything any more.
  const asked = fake.reads.length;
  await page.waitForTimeout(2_000);
  expect(fake.reads.length).toBe(asked);
});

test("a slow frame for one shared terminal never lands in another's pane, or in a conversation", async ({
  page,
}) => {
  const { fake, pane, held, aborted } = await screenWithHeldFrame(page);
  // Switch to the other terminal gpu-box shares: the same route, another grant.
  let list = await showShared(page);
  await list.getByTestId("remote-row").filter({ hasText: "Nightly build watch" }).locator("a").click();
  await expect(page).toHaveURL(new RegExp(`/remote/${GPU}/${GRANT_T2}$`));
  await expect(pane.getByTestId("remote-title")).toHaveText("Nightly build watch");
  await expect.poll(async () => (await lines(pane)).slice(0, 2)).toEqual(["$ tail -f build.log", "ok"]);
  await expect(pane.getByTestId("remote-screen-dims")).toHaveText("40 × 4");
  // Nothing of the first screen survived the switch, even before its late frame.
  await expect(pane.getByText(SCREEN_LINES[0])).toHaveCount(0);
  // The new pane's first read carried no `have`: the old frame's fingerprint did not cross over.
  expect(have(frameReads(fake, GRANT_T2)[0].search)).toBeNull();
  held.open();
  await expect.poll(() => fake.sent).toContain("late");
  await painted(page);
  expect(aborted.length).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("LATE SCREEN LINE")).toHaveCount(0);
  expect(await lines(pane)).toEqual(["$ tail -f build.log", "ok", "", ""]);
  // On to an API session on the same route: the conversation pane, with nothing of a screen.
  list = await showShared(page);
  await list.getByTestId("remote-row").filter({ hasText: "Tune the render queue" }).locator("a").click();
  await expect(pane.locator("[data-turn]")).toHaveCount(2);
  await expect(pane.getByTestId("remote-screen")).toHaveCount(0);
  await expect(page.getByText("$ tail -f build.log")).toHaveCount(0);
});

test("nothing of a peer's screen is kept in storage, and a reload restores no frame", async ({ page }) => {
  const state = { offline: false };
  await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      if (state.offline) return OFFLINE;
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const pane = await openScreen(page);
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  const stored = await page.evaluate(() =>
    JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]),
  );
  for (const secret of [SCREEN_LINES[0], "API_TOKEN", MASK, FP_A, TERM_TITLE, "gpu-box", GRANT_T])
    expect(stored, `storage holds ${secret}`).not.toContain(secret);
  // The owner drops, and the page is loaded again: there is no earlier frame to show.
  state.offline = true;
  await page.reload();
  await expect(page.getByTestId("remote-pane").getByTestId("remote-empty")).toHaveText(
    "gpu-box is not connected. Showing nothing until it is back.",
  );
  await expect(page.getByText(SCREEN_LINES[0])).toHaveCount(0);
});

// =================================================================================================
// Not a terminal, and not markup
// =================================================================================================

test("keys typed at the screen go nowhere, and it has no caret", async ({ page }) => {
  const fake = await setup(page, (f) => {
    withTerminals(f);
    f.onRead = (_kind, _grant, url) => {
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame() };
    };
  });
  const requests: { method: string; url: string }[] = [];
  page.on("request", (r) => requests.push({ method: r.method(), url: r.url() }));
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  const pane = await openScreen(page);
  const screen = pane.getByTestId("remote-screen");
  await expect(screen).toBeVisible();
  const before = await lines(pane);
  await page.evaluate(() => {
    const seen: { key: string; prevented: boolean }[] = [];
    (window as unknown as { __keys: typeof seen }).__keys = seen;
    // Bubble phase on window: by now anything that swallowed the key has done so.
    window.addEventListener("keydown", (e) => seen.push({ key: e.key, prevented: e.defaultPrevented }));
  });

  // Click into it and type what an operator would type at a terminal.
  await screen.click({ position: { x: 40, y: 40 } });
  await page.keyboard.type("rm -rf /tmp/x");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Control+c");
  await page.keyboard.press("Tab");
  await screen.focus();
  await page.keyboard.insertText("pasted text");
  await painted(page);

  // Nothing changed on the screen, nothing was written anywhere, nothing was sent.
  expect(await lines(pane)).toEqual(before);
  await expect(pane.getByText("rm -rf")).toHaveCount(0);
  await expect(pane.getByText("pasted text")).toHaveCount(0);
  expect(requests.filter((r) => r.method !== "GET")).toEqual([]);
  expect(sockets).toEqual([]);
  expect(fake.writes).toEqual([]);
  // The pane swallowed no key: each one reached the window unprevented.
  const keys = await page.evaluate(() => (window as unknown as { __keys: { key: string; prevented: boolean }[] }).__keys);
  expect(keys.map((k) => k.key)).toEqual(expect.arrayContaining(["r", "m", "Enter", "c", "Tab"]));
  expect(keys.filter((k) => k.prevented)).toEqual([]);
  // It is a group of text, not an input: nothing editable, no terminal emulator, no caret.
  await expect(screen).toHaveAttribute("role", "group");
  await expect(pane.locator("input, textarea, [contenteditable], .xterm, canvas, [role=textbox]")).toHaveCount(0);
  const facts = await screen.evaluate((el) => ({
    editable: (el as HTMLElement).isContentEditable,
    caret: getComputedStyle(el).caretColor,
    textCaret: getComputedStyle(el.querySelector("[data-testid=remote-screen-text]")!).caretColor,
    cursor: getComputedStyle(el).cursor,
    design: document.designMode,
  }));
  expect(facts).toEqual({
    editable: false,
    caret: "rgba(0, 0, 0, 0)",
    textCaret: "rgba(0, 0, 0, 0)",
    cursor: "default",
    design: "off",
  });
  // No resize was asked of anybody either: the frame reads carry `have` and `wait` and nothing else.
  for (const r of frameReads(fake))
    expect([...new URLSearchParams(r.search).keys()].sort().join()).toMatch(/^(have,)?wait$/);
});

test("hostile text in a frame is text: no markup, no link, no escape sequence, no request", async ({ page }) => {
  const hostile = [
    `<img src="https://evil.example/pixel.png" onerror="window.__pwned=1">`,
    `<script>window.__pwned=2</script><b id="planted">bold</b>`,
    `<a href="javascript:window.__pwned=3">click</a> <iframe src="https://evil.example/f"></iframe>`,
    `\\x1b[31mRED\\x1b[0m ^[[2J \\033]52;c;c2VjcmV0\\007 \\u001b[1m`,
    `https://evil.example/click-me?token=1 www.evil.example ftp://evil.example/x`,
    `[a link](https://evil.example/md) ![img](https://evil.example/md.png) <style>*{display:none}</style>`,
    `&lt;b&gt; &amp; &#x41; {{constructor.constructor('alert(1)')()}} \${alert(1)}`,
  ];
  const evil: string[] = [];
  const dialogs: string[] = [];
  page.on("dialog", (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });
  await page.route(/evil\.example/, (r) => {
    evil.push(r.request().url());
    return r.fulfill({ status: 204, body: "" });
  });
  const requests: string[] = [];
  page.on("request", (r) => requests.push(r.url()));
  // The title and the peer's name are a peer's text too.
  const hostileTitle = `<img src="https://evil.example/t.png" onerror="window.__pwned=4">[t](https://evil.example/t)`;
  const hostileName = `<b id="planted-name">gpu</b> https://evil.example/n`;
  await setup(page, (f) => {
    withTerminals(f);
    f.remote = f.remote.map((r) =>
      r.grant_id === GRANT_T ? { ...r, title: hostileTitle, peer_name: hostileName } : r,
    );
    f.onRead = (_kind, _grant, url) => {
      const h = url.searchParams.get("have");
      return h === FP_A ? unchanged(h) : { json: frame({ cols: 120, rows: 10, lines: hostile }) };
    };
  });
  const pane = await openScreen(page);
  await expect(pane.getByTestId("remote-screen")).toBeVisible();
  await expect(pane.getByTestId("remote-title")).toHaveText(hostileTitle);
  await expect(pane.getByTestId("remote-banner")).toContainText(hostileName);
  await expect(page.locator("#planted-name")).toHaveCount(0);
  // A long name a peer chose is cut, not pushed out of the head or over the title.
  await expect(pane.getByTestId("remote-title")).toBeVisible();
  const head = (await pane.locator("header").boundingBox())!;
  const badge = (await pane.locator("header").getByTestId("peer-badge").boundingBox())!;
  expect(badge.x + badge.width).toBeLessThanOrEqual(head.x + head.width + 0.5);
  // Each line is on the page exactly as it came, as text.
  expect((await lines(pane)).slice(0, hostile.length)).toEqual(hostile);
  for (const line of hostile) await expect(pane.getByText(line, { exact: true })).toBeVisible();
  // None of it became an element, a link or a style.
  const text = pane.getByTestId("remote-screen-text");
  await expect(text.locator("*:not(div)")).toHaveCount(0);
  await expect(text.locator("div > *")).toHaveCount(0);
  await expect(pane.locator("img, script, iframe, a, style, object, embed")).toHaveCount(0);
  await expect(pane.getByTestId("remote-screen").locator("b, i, u, em")).toHaveCount(0);
  await expect(page.locator("#planted")).toHaveCount(0);
  await expect(pane.getByRole("link")).toHaveCount(0);
  // The escape-looking sequence coloured nothing and cleared nothing.
  expect(
    await text.evaluate((el) => [...new Set([...el.children].map((row) => getComputedStyle(row).color))].length),
  ).toBe(1);
  await expect(pane.getByTestId("remote-title")).toBeVisible();
  // Clicking the URL goes nowhere and asks nothing.
  const here = page.url();
  await pane.getByText(hostile[4], { exact: true }).click();
  await painted(page);
  expect(page.url()).toBe(here);
  expect(page.context().pages()).toHaveLength(1);
  expect(evil, "a request to the host a frame named").toEqual([]);
  expect(requests.filter((u) => /evil\.example/.test(u))).toEqual([]);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  expect(dialogs).toEqual([]);
});
