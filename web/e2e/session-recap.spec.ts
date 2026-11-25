import { expect, test, type Locator, type Page } from "@playwright/test";
import { ACTIONS_MENU, clickHeadAction, MORE_MENU } from "./headActions";
import { setupBench } from "./terminal/harness";

// Real-browser coverage for the session-brief modal (#481): the recap icon in the terminal
// header opens a modal showing the full title, summary, and the chronological recap. Runs on
// both the desktop and mobile Playwright projects. Network + WebSocket are fully mocked via the
// terminal bench. Red→green gate: the recap icon/modal does NOT exist on origin/main, so the
// "open session brief" trigger assertion fails before the feature and passes after.

const ENGINE = "claude";
const UUID = "aaaaaaaa-1111-2222-3333-444444444444";
const TITLE = "Fix the auth token refresh-rotation race in the login flow";
const RECAP = [
  "Root-caused intermittent 401s to a token-refresh race.",
  "Added a single-flight lock + regression test (red then green).",
  "Opened PR #482 — now waiting on review.",
].join("\n");

const ROW = {
  id: `${ENGINE}:${UUID}`,
  engine: ENGINE,
  uuid: UUID,
  short_uuid: "aaaaaaaa",
  cwd: "/home/u/proj",
  // A folder ref carries the FULL cwd as `name` (projects.resolve) — the client shortens it.
  project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
  last_mtime: 1_700_000_000,
  first_user_message: "",
  title: TITLE,
  sticky: false,
  archived: false,
  ai_summary:
    "Refactoring the token-refresh path to remove a double-refresh race.",
  ai_title: TITLE,
  intervention_required: true,
  intervention_reason: "waiting on permission to edit prod config",
  reviewed_at: 1_700_000_000,
  review_excluded: false,
  has_draft: false,
  ai_recap: RECAP,
};

test.beforeEach(async ({ page }) => {
  await setupBench(page, {
    sessions: [{ engine: ENGINE, uuid: UUID, title: TITLE }],
  });
  // Override ONLY the list endpoint (not /history or /draft) with a row carrying the recap.
  // Registered after the bench route → it wins for the list call; the narrow regex leaves the
  // bench's /history + /draft handling intact.
  await page.route(/\/api\/sessions(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        sessions: [ROW],
        next_offset: null,
        total: 1,
        facets: { projects: [ROW.project], engines: [ENGINE] },
      },
    }),
  );
});

test("recap icon opens the session-brief modal with the chronological recap (#481)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);

  // The header recap icon (absent on origin/main — the red→green gate). `clickHeadAction` asserts
  // it is visible wherever the head put it and returns the control focus comes back to: the chip
  // inline, or the "Actions" trigger at ≤800px (#948 P6), whose item unmounts with the menu.
  const trigger = await clickHeadAction(page, /open session brief/i);

  const dialog = page.getByRole("dialog", { name: /fix the auth token/i });
  await expect(dialog).toBeVisible();
  // Full (untruncated) title + summary + the chronological recap timeline + intervention chip.
  await expect(dialog).toContainText(TITLE);
  await expect(dialog).toContainText("Refactoring the token-refresh path");
  await expect(dialog).toContainText("Root-caused intermittent 401s");
  await expect(dialog).toContainText("Opened PR #482");
  await expect(dialog).toContainText(/needs you/i);

  // Esc closes and returns focus to the trigger.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

// #744: the panel header is a meta run now — LED, engine box, project, update time. Red→green
// gate: on origin/main the header prints "CLAUDE // aaaaaaaa…" and a "STATUS // LIVE" readout,
// so both the engine-box assertion and the "no STATUS text" assertion fail before the change.
test("the panel header shows the LED, engine box, project and update time (#744)", async ({
  page,
}) => {
  // Pinned width: this test is about the WIDE layout, and the spec runs on the mobile project
  // too (Pixel 7 is 412px, where the update time is deliberately hidden).
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const head = page.locator('[class*="panelHead"]');
  await expect(head).toBeVisible();

  // The engine as the sidebar's short badge, the shortened cwd, and how stale the session is.
  await expect(head.locator('[class*="headEng"]')).toHaveText("cc");
  await expect(head.locator('[class*="headProject"]')).toHaveText("~/proj");
  await expect(head.locator('[class*="headUpdated"]')).toContainText(/ago/);
  // The retired chrome: no spelled-out engine, no truncated UUID, no STATUS label. These are
  // textContent assertions on purpose — the old markup must be GONE, not merely hidden.
  await expect(head).not.toContainText("STATUS");
  await expect(head).not.toContainText("CLAUDE //");
  await expect(head).not.toContainText("aaaaaaaa");
  // Link state is never colour-only.
  await expect(head.getByRole("img", { name: /^status: / })).toBeVisible();
  // Sentence-case Repaint with an icon, matching Recap / Hand off.
  await expect(
    head.getByRole("button", { name: /repaint screen/i }),
  ).toContainText("Repaint");
});

// "Only leave the buttons visible if there is no space" — the meta run yields, the actions never
// do. Asserted on VISIBILITY, not text: `toContainText` reads textContent, which still carries
// the text of a `display: none` node, so it cannot tell "collapsed" from "present".
test("the header sheds meta before the buttons as the pane narrows (#744)", async ({
  page,
  isMobile,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const head = page.locator('[class*="panelHead"]');
  const recap = head.getByRole("button", { name: /open session brief/i });
  const project = head.locator('[class*="headProject"]');
  const updated = head.locator('[class*="headUpdated"]');
  await expect(updated).toBeVisible();
  await expect(recap).toBeVisible();

  // FINE POINTER: the #744/#783 fold ladder, with its expectations unchanged. It used to be
  // driven by the VIEWPORT (420px, then 300px), but since #948 P6 any viewport ≤800px swaps the
  // chips for one "Actions" menu whatever the pointer — measured: the old viewport steps failed
  // on desktop at 420px with no Recap chip at all. So this ladder only exists on a narrow PANE in
  // a wide window now, and it is driven there: the same two widths, squeezed on the pane (the
  // idiom of the pane-width test below), waiting for a fit measured at each (#909).
  if (!isMobile) {
    await squeezePane(head, 420);
    await waitForFit(head, 420);
    // Update time is the first fact to go; the project survives because it answers "where am I".
    await expect(updated).toBeHidden();
    await expect(project).toBeVisible();
    await expect(recap).toBeVisible();

    await squeezePane(head, 300);
    await waitForFit(head, 300);
    await expect(project).toBeHidden();
    // The engine box and the LED are the floor — and every action is still reachable.
    await expect(head.locator('[class*="headEng"]')).toBeVisible();
    await expect(head.getByRole("img", { name: /^status: / })).toBeVisible();

    // #783 added a FOURTH action (Files); #859 added a fifth and sixth (the terminal quick zoom).
    // What this is really about is REACH — every action stays available at every width. On a
    // fine pointer labels stay (a 26px bar makes an icon-only chip a poor target), and labelled
    // chips have never fitted 300px, so the trailing actions fold into a "…" menu carrying their
    // full labels. The quick-zoom pair is deliberately LAST in Terminal.tsx's action array so
    // that wherever only some actions fold, it is the newest pair and never Hand off.
    const more = head.getByRole("button", { name: /more session actions/i });
    await expect(more).toBeVisible();
    await more.click();

    // Assert REACH, not position: with the menu open, every action is one tap away somewhere —
    // still on the bar, or inside the menu.
    const reachable = (name: RegExp) =>
      page
        .getByRole("menuitem", { name })
        .or(head.getByRole("button", { name }))
        .first();
    await expect(reachable(/hand off/i)).toBeVisible();
    await expect(reachable(/smaller terminal text/i)).toBeVisible();
    const recapItem = page.getByRole("menuitem", { name: /open session brief/i });
    const foldedRecap = await recapItem.isVisible().catch(() => false);
    // The menu is a portalled overlay, so a still-inline chip cannot be clicked underneath it.
    if (!foldedRecap) await page.keyboard.press("Escape");
    const openRecap = foldedRecap ? recapItem : recap;
    await expect(openRecap).toBeVisible();

    // Still a real control, not a clipped sliver: it opens the dialog at 300px.
    await openRecap.click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await squeezePane(head, null);
  }

  // A ≤800px VIEWPORT, BOTH projects (#948 P6). 420px is a REAL phone width (a Pixel 7 is 412), so
  // this is what a human actually sees. The meta run still sheds in the same order; the actions
  // are no longer chips at all — this deliberately replaces #744/#859's "every action one tap
  // away" on phones with ONE labelled "Actions" trigger whose menu carries every action. REACH is
  // still the contract: two taps, and nothing is dropped.
  await page.setViewportSize({ width: 420, height: 720 });
  // Update time is the first fact to go; the project survives because it answers "where am I".
  await expect(updated).toBeHidden();
  await expect(project).toBeVisible();
  await expectOneActionsMenu(page, head);
  // Esc closes the menu and hands focus back to the trigger.
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: ACTIONS_MENU })).toBeHidden();
  await expect(head.getByRole("button", { name: ACTIONS_MENU })).toBeFocused();

  await page.setViewportSize({ width: 300, height: 720 });
  await expect(project).toBeHidden();
  // The engine box and the LED are the floor — and every action is still reachable.
  await expect(head.locator('[class*="headEng"]')).toBeVisible();
  await expect(head.getByRole("img", { name: /^status: / })).toBeVisible();
  await expectOneActionsMenu(page, head);

  // Still a real control, not a clipped sliver: it opens the dialog at 300px.
  await page.getByRole("menuitem", { name: /open session brief/i }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

/** Squeeze (or, with `null`, restore) the pane the head lives in, leaving the viewport alone. */
async function squeezePane(head: Locator, px: number | null) {
  await head.evaluate((el, w) => {
    (el.parentElement as HTMLElement).style.width = w === null ? "" : `${w}px`;
  }, px);
}

/** #948 P6: at a ≤800px viewport the head carries NO inline action — only one labelled "Actions"
 *  trigger — and that menu carries every action under the same accessible name its chip had.
 *  Opens the menu and leaves it open. */
async function expectOneActionsMenu(page: Page, head: Locator) {
  const trigger = head.getByRole("button", { name: ACTIONS_MENU });
  await expect(trigger).toBeVisible();
  await expect(trigger).toHaveText(/actions/i);
  // No chip on the bar, and no "…" fold beside the trigger either.
  await expect(head.locator("[data-head-action]")).toHaveCount(0);
  await expect(
    head.getByRole("button", {
      name: /open session brief|hand off|terminal text|repaint|browse session files/i,
    }),
  ).toHaveCount(0);
  await expect(head.getByRole("button", { name: MORE_MENU })).toHaveCount(0);

  await trigger.click();
  const menu = page.getByRole("menu", { name: ACTIONS_MENU });
  await expect(menu).toBeVisible();
  for (const name of [
    /hand off session/i,
    /smaller terminal text/i,
    /bigger terminal text/i,
    /open session brief/i,
  ]) {
    await expect(menu.getByRole("menuitem", { name })).toBeVisible();
  }
}

// The ladder is a CONTAINER query, so it must fire on the header's own width — a wide viewport
// with a narrow pane (sidebar open, narrow split) is exactly the case a viewport media query
// would miss. Viewport stays 1400px throughout; only the pane is squeezed.
test("the collapse ladder follows the pane width, not the viewport (#744)", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1400, height: 800 });
  await page.goto(`/s/${ENGINE}/${UUID}`);
  const head = page.locator('[class*="panelHead"]');
  const project = head.locator('[class*="headProject"]');
  const updated = head.locator('[class*="headUpdated"]');
  await expect(updated).toBeVisible();

  const squeeze = (px: number) =>
    head.evaluate((el, w) => {
      (el.parentElement as HTMLElement).style.width = `${w}px`;
    }, px);

  await squeeze(430);
  await expect(updated).toBeHidden(); // viewport is still 1400 — only the pane moved
  await expect(project).toBeVisible();

  await squeeze(320);
  await expect(project).toBeHidden();
  await expect(head.locator('[class*="headEng"]')).toBeVisible();
  // Recap must stay REACHABLE at 320px — inline, or behind "…" once HeadActions' measured
  // overflow has folded it. On a fine pointer the settled state at this width IS the fold: the
  // old `expect(recap).toBeVisible()` only ever passed by polling before the ResizeObserver
  // callback ran, so a quiet box was a false green and a loaded one failed 12/12 (#909). The
  // contract is the ladder following the PANE width, not where the control physically sits.
  await expectRecapReachable(page, head, 320);
});

/** Recap is one tap away once the pane's OWN width has been measured and applied. `HeadActions`
 *  stamps the bar width its committed fit was measured at (`data-fit-width`), so waiting for the
 *  stamp to reach the squeezed width is waiting for exactly that measurement — not for the "…"
 *  trigger, which already exists at the 430px rung on a fine pointer and let a stale placement
 *  pass (Hermes on #911). In the settled placement Recap must be reachable: the inline button, or
 *  the "…" menu's item. A settled overflow that omits Recap fails here — neither exists. */
async function expectRecapReachable(page: Page, head: Locator, paneWidth: number) {
  await waitForFit(head, paneWidth);
  const recap = head.getByRole("button", { name: /open session brief/i });
  const more = head.getByRole("button", { name: /more session actions/i });
  if (await more.isVisible()) {
    await more.click();
    await expect(page.getByRole("menu")).toBeVisible();
    const item = page.getByRole("menuitem", { name: /open session brief/i });
    await expect(item.or(recap).first()).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toBeHidden();
    return;
  }
  await expect(recap).toBeVisible();
}

/** Wait for the head to commit a fit measured at the pane's squeezed width (`data-fit-width`) —
 *  "the '…' trigger exists" cannot tell a settled narrower rung from the previous wider one. */
async function waitForFit(head: Locator, paneWidth: number) {
  const stamp = head.locator("[data-fit-width]");
  await expect
    .poll(
      async () => {
        const v = await stamp.getAttribute("data-fit-width");
        return v === null || v === "unmeasured" ? Number.POSITIVE_INFINITY : Number(v);
      },
      { timeout: 15000, message: `the head never applied a fit measured at <= ${paneWidth}px` },
    )
    .toBeLessThanOrEqual(paneWidth);
}

test("the session brief carries the sidebar's identity and an ordered timeline (#744)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await clickHeadAction(page, /open session brief/i);
  const dialog = page.getByRole("dialog");

  // Everything the sidebar row shows about this session.
  await expect(dialog.locator('[class*="engTag"]')).toHaveText("cc");
  await expect(dialog).toContainText("~/proj");
  await expect(dialog).toContainText(/updated .* ago/);
  await expect(dialog).toContainText(/reviewed .* ago/);
  // The SESSION's status, resolved from the row exactly as the sidebar's dot is — this row is
  // flagged for intervention, so the dot says so rather than reporting the socket as "live".
  await expect(
    dialog.getByRole("img", {
      name: /intervention required: waiting on permission/i,
    }),
  ).toBeVisible();

  // The recap is a LIST now — one step per line, in order — not one pre-wrapped paragraph.
  const steps = dialog.getByRole("listitem");
  await expect(steps).toHaveCount(3);
  await expect(steps.nth(0)).toContainText("Root-caused intermittent 401s");
  await expect(steps.nth(2)).toContainText("Opened PR #482");
});

test("clicking the backdrop closes the session-brief modal (#481)", async ({
  page,
}) => {
  await page.goto(`/s/${ENGINE}/${UUID}`);
  await clickHeadAction(page, /open session brief/i);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // The backdrop covers the viewport; a corner click lands outside the centered/bottom dialog.
  await page.mouse.click(5, 5);
  await expect(dialog).toBeHidden();
});
