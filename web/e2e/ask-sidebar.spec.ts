/** Ask as a right-hand sidebar (#1294) — before that a page under Dashboard (#1058, #1171).
 *
 *  The operator asked for Ask one tap away on every route: an icon beside the notification bell
 *  opens a panel that slides in from the right, and the conversation in it outlives closing the
 *  panel and navigating. These specs drive what a DOM emulator cannot see: the icon's place in the
 *  corner at phone widths, the panel's geometry against the viewport edge, the chat column inside
 *  it, and that a match link really navigates the page beside it.
 */
import { expect, test, type Page } from "@playwright/test";

import { ASK_STREAM, fulfillAsk } from "./askStream";
import { openAsk } from "./askSidebar";
import { openMapFromNav } from "./mapNav";

const ANSWER = {
  answer: "Two sessions touched the upload retry.",
  matches: [
    {
      id: "claude:11111111-2222-4333-8444-555555555555",
      title: "fix flaky upload retry",
      why: "uploads.py · retry backoff",
    },
  ],
  stage: "catalog",
  configured: true,
};

async function mockShell(page: Page, { configured = true } = {}) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: [] } }));
  await page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
}

test("Ask is the icon beside the bell on every route; it slides in from the right edge (#1294)", async ({
  page,
}) => {
  await mockShell(page);
  for (const path of ["/", "/dashboard", "/mission", "/templates"]) {
    await page.goto(path);
    if ((await page.locator(".app.navOpen").count()) > 0) {
      await page.keyboard.press("Escape");
    }
    const toggle = page.locator('.hud-topbar [data-testid="ask-toggle"]');
    const bell = page.locator(".hud-topbar").getByRole("button", { name: /^Notifications/ });
    await expect(toggle).toBeVisible();
    // Beside the bell: immediately to its left, on the same line, nothing between them.
    const [tb, bb] = await Promise.all([toggle.boundingBox(), bell.boundingBox()]);
    expect(tb!.x + tb!.width).toBeLessThanOrEqual(bb!.x + 1);
    expect(bb!.x - (tb!.x + tb!.width)).toBeLessThan(12);
    expect(Math.abs(tb!.y - bb!.y)).toBeLessThan(2);
  }

  const panel = await openAsk(page);
  const vw = page.viewportSize()!.width;
  // Docked to the RIGHT edge, below the top bar.
  await expect
    .poll(async () => {
      const b = (await panel.boundingBox())!;
      return Math.round(b.x + b.width);
    })
    .toBe(vw);
  const pb = (await panel.boundingBox())!;
  expect(pb.x).toBeGreaterThan(0);
  expect(pb.y).toBeGreaterThanOrEqual(50);
  await expect(panel.getByRole("heading", { name: "Ask" })).toBeVisible();
  // Every head control holds the 44px floor (design §8).
  for (const b of await panel.getByTestId("ask-head").getByRole("button").all()) {
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  // Desktop: non-modal, so the icon that opened it closes it. Phone: a modal drawer — the page
  // (top bar included) is inert under its scrim, exactly as with the bell's drawer — so ✕ does.
  if (vw > 800) {
    await page.locator('.hud-topbar [data-testid="ask-toggle"]').click();
  } else {
    await expect(panel).toHaveAttribute("aria-modal", "true");
    await panel.getByRole("button", { name: "Close Ask" }).click();
  }
  await expect(panel).toHaveAttribute("data-open", "false");
  await expect(panel).toBeHidden();
  await expect(page.locator('.hud-topbar [data-testid="ask-toggle"]')).toBeFocused();
});

test("the dashboard's Ask button opens the sidebar, and an old /ask link lands there too (#1294)", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/dashboard");
  // No Ask field on the dashboard any more — just the button.
  await expect(page.getByTestId("dashboard-page")).toBeVisible();
  await expect(page.getByTestId("dashboard-page").getByTestId("composer-input")).toHaveCount(0);
  await page.getByTestId("dashboard-ask").click();
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");

  await page.goto("/ask");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "true");
});

test("a question answers, names the sessions it matched, and jumps into one", async ({
  page,
}) => {
  await mockShell(page);
  let asked: unknown = null;
  await page.route(ASK_STREAM, async (r) => {
    asked = r.request().postDataJSON();
    await fulfillAsk(r, ANSWER);
  });
  await page.goto("/dashboard");
  const panel = await openAsk(page);

  await panel.getByTestId("composer-input").fill("where did I fix the upload retry?");
  await panel.getByTestId("composer-input").press("Enter");

  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);
  await expect(panel.getByTestId("ask-turns")).toContainText(
    "Two sessions touched the upload retry.",
  );
  expect(asked).toMatchObject({
    query: "where did I fix the upload retry?",
    history: [],
  });

  const match = panel.getByTestId("ask-match");
  await expect(match).toContainText("uploads.py");
  const jump = match.getByRole("link", { name: /^open fix flaky upload retry$/i });
  await expect(jump).toHaveAttribute(
    "href",
    "/s/claude/11111111-2222-4333-8444-555555555555",
  );
  // The "answers are not kept" notice is gone (operator request).
  await expect(panel.getByTestId("ask-transient")).toHaveCount(0);

  // Jumping in navigates the page BESIDE the panel. On a phone the drawer covered it, so it
  // closes; on a desktop it stays open beside the session.
  await jump.click();
  await expect(page).toHaveURL(/\/s\/claude\/11111111/);
  const phone = page.viewportSize()!.width <= 800;
  await expect(panel).toHaveAttribute("data-open", phone ? "false" : "true");
});

test("a match is ONE row: the session and its reason left, the way in right", async ({
  page,
}) => {
  // The three pieces were block siblings, so "Jump in" landed under the reason as a bordered box
  // of its own — three stacked things where the operator reads one fact. Geometry, not DOM
  // nesting: a `toBeVisible` on the link passed against the stacked version, which is why this
  // measures where the boxes actually are. The mission thread draws the same row from the same
  // sheet, and `mission-turns.spec.ts` asserts it there.
  await mockShell(page);
  await page.route(ASK_STREAM, (r) => fulfillAsk(r, ANSWER));
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.goto("/dashboard");
  await openAsk(page);
  await page.getByTestId("composer-input").fill("upload retry");
  await page.getByTestId("composer-send").click();
  await expect(page.getByTestId("ask-match")).toHaveCount(1);

  const row = page.getByTestId("ask-match");
  const body = row.locator("> div").first();
  // The way in is the actions group: Open, and Open in map where the map can host a window.
  const jump = row.getByTestId("ask-match-opens");
  await expect(row.getByRole("button", { name: /in map$/ })).toHaveCount(
    page.viewportSize()!.width > 800 ? 1 : 0,
  );
  const [rb, bb, jb] = await Promise.all([
    row.boundingBox(),
    body.boundingBox(),
    jump.boundingBox(),
  ]).then((b) => b.map((x) => x!));

  // Side by side, not stacked: the link begins after the text block ends…
  expect(jb.x).toBeGreaterThanOrEqual(bb.x + bb.width);
  // …on the same line — their vertical centres agree…
  expect(Math.abs(jb.y + jb.height / 2 - (bb.y + bb.height / 2))).toBeLessThan(2);
  // …and it sits at the row's right edge rather than floating mid-row.
  expect(rb.x + rb.width - (jb.x + jb.width)).toBeLessThan(2);
  // The coarse-pointer floor survives the reflow.
  expect((await row.getByRole("link").boundingBox())!.height).toBeGreaterThanOrEqual(44);

  // A PHONE (the panel is ~92vw there) wraps it to its own line rather than crushing the title —
  // still right-aligned, and the page still does not scroll sideways.
  await page.setViewportSize({ width: 320, height: 760 });
  await page.waitForTimeout(150);
  const [rb2, bb2, jb2] = await Promise.all([
    row.boundingBox(),
    body.boundingBox(),
    jump.boundingBox(),
  ]).then((b) => b.map((x) => x!));
  expect(jb2.y).toBeGreaterThanOrEqual(bb2.y + bb2.height - 1);
  expect(rb2.x + rb2.width - (jb2.x + jb2.width)).toBeLessThan(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
});

test("the conversation outlives closing the panel and navigating; a reload ends it (#1294)", async ({
  page,
}) => {
  await mockShell(page);
  await page.route(ASK_STREAM, (r) => fulfillAsk(r, ANSWER));
  await page.goto("/dashboard");
  let panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("anything");
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);

  await panel.getByRole("button", { name: "Close Ask" }).click();
  await expect(panel).toHaveAttribute("data-open", "false");
  // In-app navigation, not a reload: the sidebar lives above the router.
  await openMapFromNav(page);
  await expect(page).toHaveURL(/\/overview$/);
  panel = await openAsk(page);
  await expect(panel.getByTestId("ask-turn")).toHaveCount(1);

  // …and nothing was persisted: a reload starts clean.
  await page.reload();
  panel = await openAsk(page);
  await expect(panel.getByTestId("ask-turn")).toHaveCount(0);
  await expect(panel.getByTestId("ask-empty")).toBeVisible();
});

test("an answer still running survives closing the panel, navigating, and reopening (#1294)", async ({
  page,
}) => {
  await mockShell(page);
  let release: (() => void) | undefined;
  const held = new Promise<void>((res) => (release = res));
  let aborted = false;
  await page.route(ASK_STREAM, async (r) => {
    await held;
    await fulfillAsk(r, ANSWER).catch(() => (aborted = true));
  });
  await page.goto("/dashboard");
  let panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("still running?");
  await panel.getByTestId("composer-input").press("Enter");
  await expect(panel.getByTestId("ask-working")).toBeVisible();

  await panel.getByRole("button", { name: "Close Ask" }).click();
  await openMapFromNav(page);
  await expect(page).toHaveURL(/\/overview$/);
  release!();
  panel = await openAsk(page);
  // The question was never aborted by closing or navigating: its answer landed in the thread.
  await expect(panel.getByTestId("ask-turns")).toContainText("Two sessions touched the upload retry.");
  await expect(panel.getByTestId("ask-working")).toHaveCount(0);
  expect(aborted).toBe(false);
});

test("the shell's mobile line decides the form: modal at <=800px, beside the page above it (#1294)", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "desktop", "drives its own widths");
  await mockShell(page);
  await page.goto("/dashboard");

  // 720px: between the bell's 640 and the shell's 800 — a 440px panel covers most of the page,
  // so it is the modal drawer: page inert, scrim, ✕ / scrim close it.
  await page.setViewportSize({ width: 720, height: 800 });
  let panel = await openAsk(page);
  await expect(panel).toHaveAttribute("aria-modal", "true");
  await expect(page.getByRole("button", { name: "Dismiss Ask" })).toBeVisible();
  await page.getByRole("button", { name: "Dismiss Ask" }).click({ position: { x: 20, y: 400 } });
  await expect(panel).toHaveAttribute("data-open", "false");

  // 1024px: non-modal. No scrim, and the page beside it still works — a nav link navigates and
  // the panel stays open over the new page.
  await page.setViewportSize({ width: 1024, height: 800 });
  panel = await openAsk(page);
  await expect(panel).not.toHaveAttribute("aria-modal");
  await expect(page.getByRole("button", { name: "Dismiss Ask" })).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "Main sections" })
    .getByRole("link", { name: "Missions", exact: true })
    .click();
  await expect(page).toHaveURL(/\/mission$/);
  await expect(panel).toHaveAttribute("data-open", "true");
  // Escape inside it closes it and gives focus back to the icon.
  await panel.getByTestId("composer-input").focus();
  await page.keyboard.press("Escape");
  await expect(panel).toHaveAttribute("data-open", "false");
  await expect(page.locator('.hud-topbar [data-testid="ask-toggle"]')).toBeFocused();
});

test("beside an open sidebar, the bell's panel opens ABOVE it and is clickable (#1294)", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "desktop", "on a phone the open drawer is modal and the bell inert");
  await mockShell(page);
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await page.locator(".hud-topbar").getByRole("button", { name: /^Notifications/ }).click();
  const bellPanel = page.getByRole("dialog", { name: "Notifications" });
  await expect(bellPanel).toBeVisible();
  const box = (await bellPanel.boundingBox())!;
  const topmost = await page.evaluate(
    ([x, y]) => document.elementFromPoint(x, y)?.closest('[role="dialog"]')?.getAttribute("aria-label"),
    [box.x + box.width / 2, box.y + Math.min(20, box.height / 2)],
  );
  expect(topmost).toBe("Notifications");
  await expect(panel).toHaveAttribute("data-open", "true");
});

test("with no AI endpoint the sidebar says what to do, instead of only greying the box", async ({
  page,
}) => {
  await mockShell(page, { configured: false });
  let called = false;
  await page.route(ASK_STREAM, (r) => {
    called = true;
    return fulfillAsk(r, ANSWER);
  });
  await page.goto("/dashboard");
  // Not on the dashboard any more (#1294) — in the sidebar.
  await expect(page.getByTestId("ask-needs-endpoint")).toHaveCount(0);
  const panel = await openAsk(page);
  await expect(panel.getByTestId("ask-needs-endpoint")).toBeVisible();
  await expect(
    panel.getByRole("link", { name: /endpoint & model/i }),
  ).toHaveAttribute("href", "/settings/ai-endpoint");
  await expect(panel.getByTestId("composer-input")).toBeDisabled();
  expect(called).toBe(false);
});

test("the mission landing no longer offers ASK — there is no mode strip left", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/mission");
  await expect(page.getByTestId("new-mission-form")).toBeVisible();
  await expect(page.getByTestId("composer-mode-ask")).toHaveCount(0);
  await expect(page.getByTestId("composer-mode-new")).toHaveCount(0);
});

/** #1069: Ask is laid out like the mission thread — a chat column, in the sidebar since #1294. The
 *  composer is docked on the panel's bottom edge at every height, the greeting fills the empty thread and gives way to the
 *  conversation, and the conversation grows UP from the composer rather than hanging from the top
 *  with a void under it. Layout is exactly what jsdom cannot see, so it is measured here. */
test("Ask is a chat column: composer docked at the bottom, the thread grows up to meet it (#1069)", async ({
  page,
}) => {
  await mockShell(page);
  const mid = "msn_" + "a".repeat(32);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      ...ANSWER,
      mission_matches: [
        {
          id: mid,
          title: "Stabilise upload retries",
          state: "done",
          project_id: "",
          why: "its instruction names the retry",
        },
      ],
    }),
  );
  await page.goto("/dashboard");
  await openAsk(page);
  const viewport = page.viewportSize()!;
  const dock = page.getByTestId("ask-form");
  const pane = page.getByTestId("ask-pane");

  // Empty: the greeting is in the thread and the composer is already on the bottom edge.
  await expect(page.getByTestId("ask-empty")).toBeVisible();
  const dockBox = (await dock.boundingBox())!;
  const paneBox = (await pane.boundingBox())!;
  expect(dockBox.y).toBeGreaterThan(paneBox.y + paneBox.height - 2);
  expect(dockBox.y + dockBox.height).toBeGreaterThan(viewport.height - 120);

  await page.getByTestId("composer-input").fill("which mission fixed the upload retry?");
  await page.getByTestId("composer-send").click();

  // The conversation replaced the greeting, as "You" then "Answer" messages…
  const turn = page.getByTestId("ask-turn");
  await expect(turn.getByRole("article", { name: "You" })).toContainText("upload retry");
  await expect(turn.getByRole("article", { name: "Answer" })).toContainText(
    "Two sessions touched",
  );
  await expect(page.getByTestId("ask-empty")).toHaveCount(0);
  // …the mission opens the mission, the session still jumps in…
  await expect(
    page.getByRole("link", { name: "Open mission Stabilise upload retries" }),
  ).toHaveAttribute("href", `/mission?m=${mid}`);
  await expect(
    page.getByRole("link", { name: "Open fix flaky upload retry" }),
  ).toBeVisible();
  // …and a SHORT thread sits against the composer, not at the top of the column.
  const threadBox = (await page.getByTestId("ask-turns").boundingBox())!;
  const after = (await dock.boundingBox())!;
  expect(after.y - (threadBox.y + threadBox.height)).toBeLessThan(60);
  expect(Math.abs(after.y - dockBox.y)).toBeLessThan(2);
});
