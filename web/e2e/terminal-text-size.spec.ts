import { expect, test, type Page } from "@playwright/test";
import { settingsPath } from "../src/routes/settingsTabs";

// #859: the terminal font size IS the agent's column count. At the shipped 13 px a phone gives
// it ~50 columns, where a column-laid-out TUI (opencode) collapses — its label column squeezed
// to three characters, its right-hand value stacked one word per line.
//
// These run in a REAL browser (mobile + desktop projects) because the thing under test is layout
// arithmetic xterm does against measured glyph metrics. jsdom reports no font metrics at all, so
// a unit test of "cols went up" there would pass against any implementation, including none.
//
// What is asserted is the CAUSE, not a DOM proxy for it: the `{t:"r",cols,rows}` frame the client
// sends to the pty. That frame is what SIGWINCHes the agent, so if it is right the relayout is
// right, and counting those frames is also what distinguishes a debounced refit from a raw one.

// WS stub that RECORDS. `sent` is every frame the client pushed; `count` is how many sockets were
// ever constructed, which is how a test sees a terminal remount it was supposed to avoid.
const RECORDING_WS = `
window.__ws = { count: 0, sent: [] };
window.WebSocket = class {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = "arraybuffer";
    window.__ws.count++;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 20);
  }
  send(data) { try { window.__ws.sent.push(JSON.parse(data)); } catch { /* binary */ } }
  close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
};
`;

async function mockApi(page: Page) {
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        auth_mode: "none",
        term_font_size: 13,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route(/\/api\/folders(\?.*)?$/, (r) =>
    r.fulfill({ json: { folders: [] } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: [] } }),
  );
}

/** Every cols value the client has told the pty about, oldest first. */
function resizes(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    (
      window as unknown as { __ws: { sent: { t: string; cols?: number }[] } }
    ).__ws.sent
      .filter((m) => m.t === "r" && typeof m.cols === "number")
      .map((m) => m.cols as number),
  );
}

/** Click a pane-head action by its accessible name, wherever HeadActions put it: inline on a
 *  wide pane, behind the "…" overflow on a phone (#783). Both are the shipped affordance. */
async function headAction(page: Page, name: RegExp) {
  const inline = page.getByRole("button", { name });
  if (await inline.isVisible().catch(() => false)) {
    await inline.click();
    return;
  }
  await page.getByRole("button", { name: /more actions|…/i }).click();
  await page.getByRole("menuitem", { name }).click();
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(RECORDING_WS);
});

test("smaller text gives the agent MORE columns (#859)", async ({ page }) => {
  await page.goto("/s/claude/termsize-cols");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(async () => (await resizes(page)).length)
    .toBeGreaterThan(0);

  const before = (await resizes(page)).at(-1) as number;
  // The default must be the narrow state this issue is about — if the baseline were already
  // wide, the assertion below would prove nothing.
  expect(before).toBeGreaterThan(0);

  await headAction(page, /smaller terminal text/i);

  // RED before this change: there is no such control at all, and no new resize frame is sent.
  await expect
    .poll(async () => (await resizes(page)).at(-1) as number)
    .toBeGreaterThan(before);
});

test("bigger text gives it fewer, and the pair is reversible (#859)", async ({
  page,
}) => {
  await page.goto("/s/claude/termsize-round");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(async () => (await resizes(page)).length)
    .toBeGreaterThan(0);
  const start = (await resizes(page)).at(-1) as number;

  await headAction(page, /smaller terminal text/i);
  await expect
    .poll(async () => (await resizes(page)).at(-1) as number)
    .toBeGreaterThan(start);

  await headAction(page, /bigger terminal text/i);
  await expect
    .poll(async () => (await resizes(page)).at(-1) as number)
    .toBe(start);
});

test("a burst of taps drags the agent through ONE width, not four (#859)", async ({
  page,
}) => {
  // The regression for the risk the issue names: one SIGWINCH per tap is the resize storm
  // #227/#349 exist to coalesce — a repaint-heavy TUI piles those frames into scrollback as
  // duplicated/garbled content. "Columns increased" cannot see this.
  //
  // The assertion counts DISTINCT widths, not frames. Measured: a single size change already
  // emits two or three frames at the SAME cols as the row count settles under the
  // ResizeObserver — pre-existing behaviour, unrelated to this control. What a raw fit()
  // per tap would produce is four DIFFERENT widths, marching the agent through every
  // intermediate size. That is the property worth pinning, and it is immune to the settling.
  //
  // A wide viewport so both actions stay inline in both projects: this test is about the
  // debounce, and the mobile overflow path is covered by the tests above.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/s/claude/termsize-burst");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(async () => (await resizes(page)).length)
    .toBeGreaterThan(0);

  // Tag the live xterm node so a remount is detectable — a rebuilt terminal is a different
  // element and loses the tag (and its scrollback with it).
  await page
    .locator(".xterm")
    .evaluate((el) => el.setAttribute("data-e2e-tag", "original"));
  const socketsBefore = await page.evaluate(
    () => (window as unknown as { __ws: { count: number } }).__ws.count,
  );
  const before = await resizes(page);

  // Four taps inside the 120ms debounce window, dispatched from the page: Playwright's
  // actionability machinery alone takes longer than the debounce, so a `.click()` loop would
  // pace the taps apart and test nothing. 25ms is short enough to stay inside the window and
  // long enough for React to re-render between taps, so each one actually steps.
  await page.evaluate(async () => {
    for (let i = 0; i < 4; i++) {
      document
        .querySelector<HTMLButtonElement>(
          'button[aria-label="Smaller terminal text"]',
        )
        ?.click();
      await new Promise((r) => setTimeout(r, 25));
    }
  });
  await page.waitForTimeout(700); // debounce + settle, with headroom on a loaded runner

  // Identity FIRST, so the tempting wrong implementation — reaching the refit by adding the
  // size to the socket effect's dep array — fails on the assertion that names what it broke
  // rather than on a downstream symptom of the rebuild.
  expect(
    await page.evaluate(
      () => (window as unknown as { __ws: { count: number } }).__ws.count,
    ),
    "a size change must not rebuild the WebSocket",
  ).toBe(socketsBefore);
  await expect(
    page.locator(".xterm"),
    "a size change must not rebuild the terminal (its scrollback goes with it)",
  ).toHaveAttribute("data-e2e-tag", "original");

  const added = (await resizes(page)).slice(before.length);
  expect(added.length, "the burst must reach the pty at all").toBeGreaterThan(
    0,
  );
  // ONE width. Four would mean every tap refit immediately.
  expect(
    new Set(added).size,
    "the agent must not be dragged through every intermediate width",
  ).toBe(1);
  expect(added[0]).toBeGreaterThan(before.at(-1) as number);
  // …and all four taps really landed (13 → 9). Without this, a React batch that collapsed the
  // burst into a single STEP would also show one width and pass while proving nothing.
  expect(
    await page.evaluate(() => localStorage.getItem("tr-termsize")),
    "each tap must step; a batched burst would show one width and prove nothing",
  ).toBe("9");
});

test("the size survives a reload, per device (#859)", async ({ page }) => {
  await page.goto("/s/claude/termsize-persist");
  await expect(page.locator(".xterm")).toBeVisible();
  await expect
    .poll(async () => (await resizes(page)).length)
    .toBeGreaterThan(0);
  const baseline = (await resizes(page)).at(-1) as number;

  await headAction(page, /smaller terminal text/i);
  await expect
    .poll(async () => (await resizes(page)).at(-1) as number)
    .toBeGreaterThan(baseline);

  // The server still says 13 px (see mockApi) — the device cache must win, or a phone would be
  // dragged back to the desktop's size on every reload.
  await page.reload();
  await expect(page.locator(".xterm")).toBeVisible();

  // Anchored on what is durable, not on the exact column count: a fresh load fits at the new
  // size from the very first paint rather than reflowing into it, so it can legitimately land
  // a column either side of the pre-reload value (measured: 127 before, 128 after). What must
  // hold is that the choice survived and the terminal is still WIDER than the 13 px baseline.
  expect(await page.evaluate(() => localStorage.getItem("tr-termsize"))).toBe(
    "12",
  );
  await expect
    .poll(async () => (await resizes(page)).at(-1) as number)
    .toBeGreaterThan(baseline);
});

test("Settings' stepper meets the 44px touch floor and disables at the clamps (#859)", async ({
  page,
}) => {
  await page.goto(settingsPath("appearance"));
  const smaller = page.getByRole("button", { name: /smaller terminal text/i });
  const bigger = page.getByRole("button", { name: /bigger terminal text/i });
  await expect(smaller).toBeVisible();

  // Computed geometry, not the CSS declaration: a 44px rule a flex parent then squashes still
  // reads as 44px in the stylesheet and taps like 30px under a thumb.
  for (const b of [smaller, bigger]) {
    const box = await b.boundingBox();
    expect(box, "the stepper button must be laid out").not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }

  // Walk to the floor: the control must stop rather than wrap or go unreadable.
  await expect(bigger).toBeEnabled();
  for (let i = 0; i < 8; i++) {
    if (await smaller.isDisabled()) break;
    await smaller.click();
  }
  await expect(smaller).toBeDisabled();
  await expect(bigger).toBeEnabled();
  await expect(page.getByText(/^8 px$/)).toBeVisible();

  // Reset is the way back from an unreadable size, so it must work from the floor.
  await page.getByRole("button", { name: /reset to 13 px/i }).click();
  await expect(page.getByText(/^13 px$/)).toBeVisible();
  await expect(smaller).toBeEnabled();
});

test("the stepper is reachable and operable by keyboard (#859)", async ({
  page,
}) => {
  await page.goto(settingsPath("appearance"));
  const smaller = page.getByRole("button", { name: /smaller terminal text/i });
  await smaller.focus();
  await expect(smaller).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/^12 px$/)).toBeVisible();
  // Focus stays on the button across the step, which is what makes the aria-live readout the
  // only feedback a screen-reader user gets — and why the value carries one.
  await expect(smaller).toBeFocused();
});
