/** `/ask` as a page (#1058).
 *
 *  Ask was a MODE of the mission composer: to reach it you entered Missions and pressed a segmented
 *  control, and there was no URL that opened it. These specs drive the thing that changed — you get
 *  there from the nav, the URL is real, and the answer still carries the match rows and the way
 *  into the session it names.
 *
 *  Real browser rather than jsdom because the entry point is a nav link whose label the CSS clips
 *  at phone widths: the interesting question is whether it is still clickable and still named, and
 *  a DOM emulator models neither the clip nor the layout that motivates it.
 */
import { expect, test, type Page } from "@playwright/test";

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

/** The bar's nav, at every width — the drawer renders the same five links, so an unscoped
 *  `getByRole` is ambiguous on a phone with the drawer open. */
const barNav = (page: Page) =>
  page.getByRole("navigation", { name: "Main sections" });

test("ASK is one tap from the top bar at every width, and the page is its own URL", async ({
  page,
}) => {
  await mockShell(page);
  await page.goto("/");
  // Named even where the label is visually clipped (mobile): the accessible name is the link's own
  // text, which the CSS clips rather than removes.
  const link = barNav(page).getByRole("link", { name: "Dashboard", exact: true });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId("ask-page")).toBeVisible();
  await expect(link).toHaveAttribute("aria-current", "page");

  // …and a direct load of that URL works, which is what having a route means.
  await page.goto("/dashboard");
  await expect(page.getByTestId("ask-page")).toBeVisible();
  // #1123: Ask's old path keeps working — it lands on the dashboard, where Ask lives now.
  await page.goto("/ask");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId("ask-page")).toBeVisible();
  await expect(link).toHaveAttribute("aria-current", "page");
});

test("a question answers, names the sessions it matched, and jumps into one", async ({
  page,
}) => {
  await mockShell(page);
  let asked: unknown = null;
  await page.route("**/api/pulse/ask", async (r) => {
    asked = r.request().postDataJSON();
    await r.fulfill({ json: ANSWER });
  });
  await page.goto("/ask");

  await page.getByTestId("composer-input").fill("where did I fix the upload retry?");
  await page.getByTestId("composer-send").click();

  await expect(page.getByTestId("ask-turn")).toHaveCount(1);
  await expect(page.getByTestId("ask-turns")).toContainText(
    "Two sessions touched the upload retry.",
  );
  expect(asked).toMatchObject({
    query: "where did I fix the upload retry?",
    history: [],
  });

  // The match row carries WHY it matched and a way in. An answer naming a session the operator
  // cannot open is half an answer.
  const match = page.getByTestId("ask-match");
  await expect(match).toContainText("uploads.py");
  await expect(
    match.getByRole("link", { name: /jump into fix flaky upload retry/i }),
  ).toHaveAttribute(
    "href",
    "/s/claude/11111111-2222-4333-8444-555555555555",
  );

  // And the page says the answers are not kept, before the operator finds out by reloading.
  await expect(page.getByTestId("ask-transient")).toContainText(/not kept/i);
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
  await page.route("**/api/pulse/ask", (r) => r.fulfill({ json: ANSWER }));
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.goto("/ask");
  await page.getByTestId("composer-input").fill("upload retry");
  await page.getByTestId("composer-send").click();
  await expect(page.getByTestId("ask-match")).toHaveCount(1);

  const row = page.getByTestId("ask-match");
  const body = row.locator("> div").first();
  const jump = row.getByRole("link");
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
  expect(jb.height).toBeGreaterThanOrEqual(44);

  // A PHONE wraps it to its own line rather than crushing the title — still right-aligned, and
  // the page still does not scroll sideways.
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

test("leaving and coming back starts clean — the answers really are transient", async ({
  page,
}) => {
  await mockShell(page);
  await page.route("**/api/pulse/ask", (r) => r.fulfill({ json: ANSWER }));
  await page.goto("/ask");
  await page.getByTestId("composer-input").fill("anything");
  await page.getByTestId("composer-send").click();
  await expect(page.getByTestId("ask-turn")).toHaveCount(1);

  // In-app navigation, not a reload: the claim is about the PAGE's lifetime, and a reload would
  // prove something weaker (that nothing was persisted server-side).
  await openMapFromNav(page);
  await expect(page).toHaveURL(/\/overview$/);
  await barNav(page).getByRole("link", { name: "Dashboard", exact: true }).click();
  await expect(page.getByTestId("ask-page")).toBeVisible();
  await expect(page.getByTestId("ask-turn")).toHaveCount(0);
});

test("with no AI endpoint the page says what to do, instead of only greying the box", async ({
  page,
}) => {
  await mockShell(page, { configured: false });
  let called = false;
  await page.route("**/api/pulse/ask", (r) => {
    called = true;
    return r.fulfill({ json: ANSWER });
  });
  await page.goto("/ask");
  await expect(page.getByTestId("ask-needs-endpoint")).toBeVisible();
  await expect(
    page.getByRole("link", { name: /endpoint & model/i }),
  ).toHaveAttribute("href", "/settings/ai-endpoint");
  await expect(page.getByTestId("composer-input")).toBeDisabled();
  expect(called).toBe(false);
});

test("the mission landing no longer offers ASK — there is no mode strip left", async ({
  page,
}) => {
  // The other half of the move: if the segmented control survived, the app would have two Asks,
  // one of them keeping its answers somewhere the operator cannot find them again.
  await mockShell(page);
  await page.goto("/mission");
  await expect(page.getByTestId("new-mission-form")).toBeVisible();
  await expect(page.getByTestId("composer-mode-ask")).toHaveCount(0);
  await expect(page.getByTestId("composer-mode-new")).toHaveCount(0);
});

/** #1069: Ask is laid out like the mission thread — a chat column. The composer is docked on the
 *  bottom edge at every height, the greeting fills the empty thread and gives way to the
 *  conversation, and the conversation grows UP from the composer rather than hanging from the top
 *  with a void under it. Layout is exactly what jsdom cannot see, so it is measured here. */
test("Ask is a chat column: composer docked at the bottom, the thread grows up to meet it (#1069)", async ({
  page,
}) => {
  await mockShell(page);
  const mid = "msn_" + "a".repeat(32);
  await page.route("**/api/pulse/ask", (r) =>
    r.fulfill({
      json: {
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
      },
    }),
  );
  await page.goto("/ask");
  const viewport = page.viewportSize()!;
  const dock = page.getByTestId("ask-form");
  const pane = page.getByTestId("ask-pane");

  // Empty: the greeting is in the thread and the composer is already on the bottom edge.
  await expect(page.getByRole("heading", { name: "BattleLab dashboard" })).toBeVisible();
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
  await expect(page.getByRole("heading", { name: "BattleLab dashboard" })).toHaveCount(0);
  // …the mission opens the mission, the session still jumps in…
  await expect(
    page.getByRole("link", { name: "Open mission Stabilise upload retries" }),
  ).toHaveAttribute("href", `/mission?m=${mid}`);
  await expect(
    page.getByRole("link", { name: "Jump into fix flaky upload retry" }),
  ).toBeVisible();
  // …and a SHORT thread sits against the composer, not at the top of the column.
  const threadBox = (await page.getByTestId("ask-turns").boundingBox())!;
  const after = (await dock.boundingBox())!;
  expect(after.y - (threadBox.y + threadBox.height)).toBeLessThan(60);
  expect(Math.abs(after.y - dockBox.y)).toBeLessThan(2);
});
