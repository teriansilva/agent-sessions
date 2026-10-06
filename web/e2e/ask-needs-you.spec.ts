/** ASK as the home for sessions without a mission (#1086 Phase 3): RECENT WORK, NEEDS YOU and the
 *  details dialog on the dashboard, and the needs-you marker on Ask's answers (its own page in
 *  #1171, the right-hand sidebar since #1294).
 *
 *  Real browser, API mocked. What is pinned is what the operator relies on: Approve names what it
 *  does and approves exactly that decision; the EDITED text is what is sent; opening details opens
 *  no terminal socket (a viewer attach is what failed Approve in #1049); a moved screen sends
 *  nothing and says so; a late answer cannot repaint a newer filter; "Nothing needs you" appears
 *  only after a successful read; and an answer about a session that needs you says so.
 */
import { expect, test, type Page, type Request } from "@playwright/test";

import { ASK_STREAM, fulfillAsk } from "./askStream";
import { openAsk } from "./askSidebar";

const A = "claude:aaaaaaaa-0000-4000-8000-00000000000a";
const B = "claude:aaaaaaaa-0000-4000-8000-00000000000b";
const C = "codex:aaaaaaaa-0000-4000-8000-00000000000c";
const NOW = Math.floor(Date.now() / 1000);
const MENU = {
  engine: "claude",
  question: "How should I proceed?",
  options: [
    { n: 1, label: "Keep the retry", selected: true },
    { n: 2, label: "Delete it", selected: false },
  ],
};

function row(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    engine: id.split(":")[0],
    title: `session ${id.slice(-1)}`,
    project: { id: id.startsWith("codex") ? "p2" : "p1", name: id.startsWith("codex") ? "Beta" : "Alpha" },
    last_activity: NOW - 60,
    since: NOW - 120,
    reason: "waiting on you",
    summary: "",
    flagged: true,
    kind: "question",
    menu: null,
    action: null,
    ...over,
  };
}

const ROWS = [
  row(A, {
    kind: "choice",
    menu: MENU,
    since: NOW - 60,
    action: { id: "act-choose", verb: "choose", state: "proposed", option: 2, can_approve: true, can_reject: true, menu: MENU },
  }),
  row(B, {
    kind: "question",
    since: NOW - 300,
    action: { id: "act-text", verb: "answer", state: "proposed", can_approve: true, can_reject: true },
  }),
  row(C, { kind: "question", since: NOW - 900 }),
];

function payload(rows = ROWS) {
  return {
    rows,
    total: rows.length,
    total_unfiltered: ROWS.length,
    needs_you_ids: ROWS.map((r) => r.id),
    truncated: false,
    facets: { engines: ["claude", "codex"], projects: [{ id: "p1", name: "Alpha" }, { id: "p2", name: "Beta" }] },
    window_days: 1,
  };
}

const RECAP = {
  window_days: 1,
  source: "ai",
  generated_at: NOW - 240,
  stale: false,
  configured: true,
  error: null,
  entries: [0, 1, 2, 3, 4, 5].map((i) => ({
    session_key: i % 2 ? C : A,
    ts: NOW - 86_000 + i * 14_000,
    text: `step ${i}`,
    engine: i % 2 ? "codex" : "claude",
    title: `work ${i}`,
    project: i % 2 ? { id: "p2", name: "Beta" } : { id: "p1", name: "Alpha" },
    session_recap: `recap of ${i}`,
  })),
};

async function mockShell(page: Page) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        auth_mode: "none",
        username: null,
        terminal_backend: "ws",
        pulse: { configured: true, window_days: 1 },
        new_session_engines: ["claude"],
        onboarded: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], total: 0, next_offset: null, facets: { projects: [], engines: [] } } }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  await page.route(/\/api\/pulse\/recap(\?.*)?$/, (r) => r.fulfill({ json: RECAP }));
}

/** NEEDS YOU + its details/actions. Returns the requests the page made, for assertions. */
async function mockNeedsYou(
  page: Page,
  opts: { list?: (url: URL) => unknown | Promise<unknown>; lastWords?: string } = {},
) {
  const seen = { lists: [] as URL[], approves: [] as Request[], dismisses: [] as Request[] };
  await page.route(/\/api\/pulse\/needs-you(\?.*)?$/, async (r) => {
    const url = new URL(r.request().url());
    seen.lists.push(url);
    const body = opts.list ? await opts.list(url) : payload();
    if (body && typeof body === "object" && "delayMs" in (body as object)) {
      // A SLOW answer: held inside the handler (a fulfil scheduled after the handler returns is
      // treated as abandoned and never completes). Playwright runs handlers concurrently, so a
      // later request is answered while this one is still waiting.
      const { delayMs, json } = body as { delayMs: number; json: unknown };
      await new Promise((res) => setTimeout(res, delayMs));
      return r.fulfill({ json }).catch(() => {});
    }
    if (body && typeof body === "object" && "status" in (body as object)) {
      return r.fulfill(body as { status: number; json: unknown });
    }
    return r.fulfill({ json: body });
  });
  await page.route(/\/api\/pulse\/needs-you\/.+\/details$/, (r) => {
    const id = decodeURIComponent(new URL(r.request().url()).pathname.split("/")[4]);
    const found = ROWS.find((x) => x.id === id)!;
    const action = found.action
      ? {
          ...found.action,
          ...(found.action.verb === "answer"
            ? { editable: true, suggested_text: "Use the second option." }
            : {}),
        }
      : null;
    return r.fulfill({
      json: {
        id,
        title: found.title,
        engine: found.engine,
        project: found.project,
        reason: found.reason,
        last_words: opts.lastWords ?? "Which option do you want?",
        screen: "? How should I proceed?\n  1. Keep the retry\n  2. Delete it",
        prompt_class: "choice",
        menu: found.menu,
        action,
      },
    });
  });
  await page.route(/\/api\/pulse\/needs-you\/.+\/dismiss$/, (r) => {
    seen.dismisses.push(r.request());
    return r.fulfill({ json: { dismissed: true, rejected: true } });
  });
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) => {
    seen.approves.push(r.request());
    return r.fulfill({ json: { id: "x", state: "delivered" } });
  });
  return seen;
}

const needs = (page: Page) => page.getByTestId("needs-you");

test("RECENT WORK previews the latest four, oldest first, and Show more opens the window by day", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  await page.goto("/dashboard");
  const preview = page.getByTestId("recent-work").getByTestId("recent-work-entry");
  await expect(preview).toHaveCount(4);
  // The LATEST four (2..5), shown oldest first.
  await expect(preview.first()).toContainText("step 2");
  await expect(preview.last()).toContainText("step 5");

  await page.getByTestId("recent-work-more").click();
  const dialog = page.getByTestId("recent-work-dialog");
  await expect(dialog.getByTestId("recent-work-entry")).toHaveCount(6);
  await dialog.getByLabel("Agent").selectOption("codex");
  await expect(dialog.getByTestId("recent-work-entry")).toHaveCount(3);
  await dialog.getByRole("button", { name: /show this session’s recap/i }).first().click();
  await expect(dialog.getByText("recap of 1")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("NEEDS YOU lists only what needs you, and every Approve names what it does", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  await page.goto("/dashboard");
  const rows = needs(page).getByTestId("needs-you-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0).getByTestId("needs-you-approve")).toHaveText("Approve · Delete it");
  await expect(rows.nth(1).getByTestId("needs-you-approve")).toHaveText("Approve · send");
  // A question with nothing to approve offers details only — never a guessed button.
  await expect(rows.nth(2).getByTestId("needs-you-approve")).toHaveCount(0);
  await expect(rows.nth(2).getByTestId("needs-you-details")).toBeVisible();
  // ≥ 44px touch targets on every control (§8).
  for (const b of await needs(page).getByRole("button").all()) {
    expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
});

test("Approve on a row approves exactly that decision, unedited, and re-reads the list", async ({ page }) => {
  await mockShell(page);
  const seen = await mockNeedsYou(page);
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(3);
  const before = seen.lists.length;
  await needs(page).getByTestId("needs-you-approve").first().click();
  await expect.poll(() => seen.approves.length).toBe(1);
  expect(seen.approves[0].url()).toContain("/api/pulse/actions/act-choose/approve");
  expect(seen.approves[0].postDataJSON()).toEqual({});
  await expect.poll(() => seen.lists.length).toBeGreaterThan(before);
});

test("details: the EDITED text is what gets sent, and opening them opens no terminal socket", async ({ page }) => {
  await mockShell(page);
  const seen = await mockNeedsYou(page);
  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").nth(1).getByTestId("needs-you-details").click();
  const dialog = page.getByTestId("needs-you-dialog");
  await expect(dialog.getByTestId("needs-you-last-words")).toHaveText("Which option do you want?");
  // A text decision: the screen would only repeat the last words + the agent's input box (#1169).
  await expect(dialog.getByTestId("needs-you-screen")).toHaveCount(0);
  const box = dialog.getByTestId("needs-you-text");
  await expect(box).toHaveValue("Use the second option.");
  await box.fill("Use option two, and add a unit test.");
  await dialog.getByTestId("needs-you-dialog-approve").click();
  await expect.poll(() => seen.approves.length).toBe(1);
  expect(seen.approves[0].url()).toContain("/api/pulse/actions/act-text/approve");
  expect(seen.approves[0].postDataJSON()).toEqual({ text: "Use option two, and add a unit test." });
  await expect(dialog).toHaveCount(0);
  expect(sockets.filter((u) => u.includes("/ws"))).toEqual([]);
});

test("details: an UNEDITED text approve sends no text at all", async ({ page }) => {
  await mockShell(page);
  const seen = await mockNeedsYou(page);
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").nth(1).getByTestId("needs-you-details").click();
  await page.getByTestId("needs-you-dialog").getByTestId("needs-you-dialog-approve").click();
  await expect.poll(() => seen.approves.length).toBe(1);
  expect(seen.approves[0].postDataJSON()).toEqual({});
});

test("a moved screen sends nothing, says so, and shows where the session is now", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  let details = 0;
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) =>
    r.fulfill({ status: 409, json: { detail: "the screen changed" } }),
  );
  page.on("request", (r) => {
    if (/\/details$/.test(r.url())) details++;
  });
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").first().getByTestId("needs-you-details").click();
  const dialog = page.getByTestId("needs-you-dialog");
  await dialog.getByTestId("needs-you-dialog-approve").click();
  await expect(dialog.getByTestId("needs-you-error")).toContainText("moved on");
  await expect(dialog).toBeVisible();
  await expect.poll(() => details).toBeGreaterThanOrEqual(2); // re-read, never act on a frozen screen
});

test("Dismiss rejects that decision and never names a screen", async ({ page }) => {
  await mockShell(page);
  const seen = await mockNeedsYou(page);
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").first().getByTestId("needs-you-details").click();
  await page.getByTestId("needs-you-dialog").getByTestId("needs-you-dismiss").click();
  await expect.poll(() => seen.dismisses.length).toBe(1);
  expect(decodeURIComponent(seen.dismisses[0].url())).toContain(`/needs-you/${A}/dismiss`);
  expect(seen.dismisses[0].postDataJSON()).toEqual({ action_id: "act-choose" });
});

test("a filter change cannot be painted over by a late answer for the old filter", async ({ page }) => {
  // The race, deliberately: the first filter's answer is SLOW, the second filter's is fast, so the
  // stale answer lands LAST. Only the newest question may paint (the generation guard).
  await mockShell(page);
  await mockNeedsYou(page, {
    list: async (url) => {
      const engine = url.searchParams.get("engine");
      if (engine === "claude") return { delayMs: 2500, json: payload([ROWS[0], ROWS[1]]) };
      return engine === "codex" ? payload([ROWS[2]]) : payload();
    },
  });
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(3); // options loaded
  const slow = page.waitForRequest((r) => r.url().includes("engine=claude"));
  await needs(page).getByLabel("Agent").selectOption("claude");
  await slow; // the slow answer is genuinely IN FLIGHT before the next question is asked…
  await needs(page).getByLabel("Agent").selectOption("codex"); // …and superseded
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(1);
  await page.waitForTimeout(3000); // the stale "claude" answer lands now
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(1);
  await expect(needs(page).getByTestId("needs-you-row")).toHaveAttribute("data-session", C);
});

test("'Nothing needs you' only after a successful read; a failed read is its own state", async ({ page }) => {
  await mockShell(page);
  let fail = true;
  await mockNeedsYou(page, {
    list: () => (fail ? { status: 503, json: { detail: "ledger unreadable" } } : payload([])),
  });
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-error")).toBeVisible();
  await expect(needs(page).getByTestId("needs-you-empty")).toHaveCount(0);
  fail = false;
  await needs(page).getByRole("button", { name: "Retry" }).click();
  await expect(needs(page).getByTestId("needs-you-empty")).toHaveText("Nothing needs you.");
});

test("after asking, answer rows mark who needs you and open the same details (#1086, #1171)", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "One relay session is stuck.",
      matches: [{ id: A, title: "session a", why: "asked which option" }],
      stage: "catalog",
      configured: true,
    }),
  );
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(3);
  // Ask is the sidebar (#1294): the list stays on the dashboard beside it, the marker comes along.
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("is anything stuck?");
  await panel.getByTestId("composer-send").click();
  await expect(page).toHaveURL(/\/dashboard$/);

  const match = panel.getByTestId("ask-match").first();
  await expect(match.getByTestId("ask-match-needs-you")).toHaveText("Needs you");
  await match.getByRole("button", { name: /details for session a/i }).click();
  await expect(page.getByTestId("needs-you-dialog")).toBeVisible();
  await page.keyboard.press("Escape");

  await expect(page.getByTestId("needs-you-dialog")).toHaveCount(0);
  // One Escape closed the dialog, not the sidebar under it.
  await expect(panel).toHaveAttribute("data-open", "true");
  await panel.getByRole("button", { name: "Close Ask" }).click();
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(3);
});

test("dismissing the details dialog by its backdrop closes ONLY the dialog, not the Ask drawer under it (#1294)", async ({ page }) => {
  // Hermes on #1296: on a phone the Ask sidebar is a modal drawer, and the details dialog it opens
  // is portalled to <body> — so a press on the dialog's backdrop read as "outside the drawer" and
  // one tap closed both. One dismissal closes the top surface only.
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "One relay session is stuck.",
      matches: [{ id: A, title: "session a", why: "asked which option" }],
      stage: "catalog",
      configured: true,
    }),
  );
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("is anything stuck?");
  await panel.getByTestId("composer-send").click();
  await panel.getByRole("button", { name: /details for session a/i }).click();
  const dialog = page.getByTestId("needs-you-dialog");
  await expect(dialog).toBeVisible();

  // A press on the backdrop, clear of the dialog box.
  const vp = page.viewportSize()!;
  await page.mouse.click(4, vp.height - 4);
  await expect(dialog).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-open", "true");
  await expect(panel.getByTestId("ask-match").first()).toBeVisible();
});

test("Open session from Ask's details dialog lands on the session — on a phone the drawer gets out of the way (#1294)", async ({ page }) => {
  // Hermes on #1296: the drawer closed for links INSIDE the aside, but the details dialog is its
  // own portal, so its Open session navigated while the modal drawer stayed over the session.
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "One relay session is stuck.",
      matches: [{ id: A, title: "session a", why: "asked which option" }],
      stage: "catalog",
      configured: true,
    }),
  );
  await page.goto("/dashboard");
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("is anything stuck?");
  await panel.getByTestId("composer-send").click();
  await panel.getByRole("button", { name: /details for session a/i }).click();
  const dialog = page.getByTestId("needs-you-dialog");
  await dialog.getByRole("link", { name: "Open session" }).click();
  await expect(page).toHaveURL(/\/s\/claude\/aaaaaaaa-0000-4000-8000-00000000000a$/);
  await expect(dialog).toHaveCount(0);
  const phone = page.viewportSize()!.width <= 800;
  await expect(panel).toHaveAttribute("data-open", phone ? "false" : "true");
  // Closing is not discarding: the conversation is still there when Ask is reopened.
  const again = await openAsk(page);
  await expect(again.getByTestId("ask-turn")).toHaveCount(1);
});

// ---- review 5184 regressions --------------------------------------------------------------------

test("an approval that completes AFTER a filter change never repaints the old filter", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page, {
    list: (url) => (url.searchParams.get("engine") === "codex" ? payload([ROWS[2]]) : payload()),
  });
  let release!: () => void;
  const held = new Promise<void>((res) => (release = res));
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, async (r) => {
    await held; // the approval is still in flight…
    return r.fulfill({ json: { id: "x", state: "delivered" } });
  });
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(3);
  await needs(page).getByTestId("needs-you-approve").first().click();
  await needs(page).getByLabel("Agent").selectOption("codex"); // …when the operator filters
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(1);
  release();
  await page.waitForTimeout(800);
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(1);
  await expect(needs(page).getByTestId("needs-you-row")).toHaveAttribute("data-session", C);
});

test("a dialog cannot be dismissed, or its text edited, while its decision is in flight", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  let release!: () => void;
  const held = new Promise<void>((res) => (release = res));
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, async (r) => {
    await held;
    return r.fulfill({ json: { id: "x", state: "delivered" } });
  });
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").nth(1).getByTestId("needs-you-details").click();
  const dialog = page.getByTestId("needs-you-dialog");
  await dialog.getByTestId("needs-you-dialog-approve").click();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Close details" })).toBeDisabled();
  await expect(dialog.getByTestId("needs-you-text")).toHaveAttribute("readonly", "");
  release();
  await expect(dialog).toHaveCount(0); // it closes itself when its own decision settles
});

test("a long NEEDS YOU list scrolls inside the dashboard, never the document", async ({ page }) => {
  await mockShell(page);
  const many = Array.from({ length: 30 }, (_, i) =>
    row(`claude:aaaaaaaa-0000-4000-8000-${String(i).padStart(12, "0")}`, { title: `row ${i}` }),
  );
  await mockNeedsYou(page, { list: () => payload(many) });
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-row").first()).toBeVisible();
  // The docked Ask field is gone (#1294); what it pinned still holds — the page scrolls in its own
  // pane, and the document (and with it the top bar) never moves.
  const vh = page.viewportSize()!.height;
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(vh);
  const pane = page.getByTestId("dashboard-pane");
  expect(await pane.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
});

test("a failed poll after an EMPTY read is shown as a failure, never as 'Nothing needs you'", async ({ page }) => {
  await page.clock.install();
  await mockShell(page);
  let fail = false;
  await mockNeedsYou(page, {
    list: () => (fail ? { status: 503, json: { detail: "ledger unreadable" } } : payload([])),
  });
  await page.goto("/dashboard");
  await expect(needs(page).getByTestId("needs-you-empty")).toBeVisible();
  fail = true;
  await page.clock.runFor(31_000); // the 30 s poll
  await expect(needs(page).getByTestId("needs-you-refresh-error")).toBeVisible();
  await expect(needs(page).getByTestId("needs-you-empty")).toHaveCount(0);
});

test("a list filter never hides the needs-you marker on an answer", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page, {
    list: (url) => {
      const body = url.searchParams.get("engine") === "codex" ? payload([ROWS[2]]) : payload();
      return { ...body, total_unfiltered: 3, needs_you_ids: ROWS.map((r) => r.id) };
    },
  });
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, { answer: "A claude session waits.", matches: [{ id: A, title: "session a", why: "choice" }], stage: "catalog", configured: true }),
  );
  await page.goto("/dashboard");
  await needs(page).getByLabel("Agent").selectOption("codex");
  await expect(needs(page).getByTestId("needs-you-row")).toHaveCount(1);
  const panel = await openAsk(page);
  await panel.getByTestId("composer-input").fill("anything stuck?");
  await panel.getByTestId("composer-send").click();
  const match = panel.getByTestId("ask-match").first();
  await expect(match.getByTestId("ask-match-needs-you")).toBeVisible();
  await expect(match.getByRole("button", { name: /details for session a/i })).toBeVisible();
});

test("a failed read for a NEW window is shown as a failure, never as the old window's result", async ({ page }) => {
  // Review 5188: the 1-day recap stayed on screen, looking healthy, under a 3-day selection whose
  // read had failed.
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(/\/api\/pulse\/recap\?window_days=3$/, (r) =>
    r.fulfill({ status: 500, json: { detail: "boom" } }),
  );
  await page.goto("/dashboard");
  const recent = page.getByTestId("recent-work");
  await expect(recent.getByTestId("recent-work-entry")).toHaveCount(4);
  await recent.getByText("3", { exact: true }).click();
  await expect(recent.getByTestId("recent-work-error")).toBeVisible();
  await expect(recent.getByTestId("recent-work-entry")).toHaveCount(0);
  await expect(recent.getByTestId("recent-work-more")).toHaveCount(0);
});

test("the Show more dialog follows a window change, and a failed read there is a failure too", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(/\/api\/pulse\/recap\?window_days=2$/, (r) =>
    r.fulfill({ status: 500, json: { detail: "boom" } }),
  );
  await page.goto("/dashboard");
  await page.getByTestId("recent-work-more").click();
  const dialog = page.getByTestId("recent-work-dialog");
  await expect(dialog.getByTestId("recent-work-entry")).toHaveCount(6);
  await dialog.getByText("2", { exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("recent-work-error")).toBeVisible();
  await expect(dialog.getByTestId("recent-work-entry")).toHaveCount(0);
});

test("a slow refresh for the old window never leaves the new window 'updating…'", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(/\/api\/pulse\/recap\?window_days=1$/, (r) =>
    r.fulfill({ json: { ...RECAP, stale: true } }),
  );
  await page.route(/\/api\/pulse\/recap\?window_days=3$/, (r) =>
    r.fulfill({ json: { ...RECAP, window_days: 3 } }),
  );
  let refreshes = 0;
  await page.route(/\/api\/pulse\/recap$/, async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    refreshes++;
    await new Promise((res) => setTimeout(res, 1500));
    return r.fulfill({ json: RECAP }).catch(() => {});
  });
  await page.goto("/dashboard");
  const recent = page.getByTestId("recent-work");
  await expect(recent.getByText("updating…")).toBeVisible();
  await recent.getByText("3", { exact: true }).click();
  await expect(recent.getByText("updating…")).toHaveCount(0);
  await expect.poll(() => refreshes).toBe(1);
  await page.waitForTimeout(2000); // the old refresh has settled by now
  await expect(recent.getByText("updating…")).toHaveCount(0);
  await expect(recent.getByTestId("recent-work-entry")).toHaveCount(4);
});

test("after a conflict, a FAILED reload marks the old screen out of date and allows no decision", async ({ page }) => {
  // Review 5188: the frozen screen kept its "On screen now" label and live controls.
  await mockShell(page);
  await mockNeedsYou(page);
  await page.route(/\/api\/pulse\/actions\/[^/]+\/approve$/, (r) =>
    r.fulfill({ status: 409, json: { detail: "the screen changed" } }),
  );
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").nth(1).getByTestId("needs-you-details").click();
  const dialog = page.getByTestId("needs-you-dialog");
  await expect(dialog.getByTestId("needs-you-dialog-approve")).toBeVisible();
  // Every later details read fails.
  let fail = true;
  await page.route(/\/api\/pulse\/needs-you\/.+\/details$/, (r) =>
    fail ? r.fulfill({ status: 500, json: { detail: "boom" } }) : r.fallback(),
  );
  await dialog.getByTestId("needs-you-dialog-approve").click();
  const outdated = dialog.getByTestId("needs-you-outdated");
  await expect(outdated).toContainText("couldn’t be read");
  await expect(dialog.getByText("On screen now", { exact: false })).toHaveCount(0);
  await expect(dialog.getByText("out of date", { exact: false }).first()).toBeVisible();
  await expect(dialog.getByTestId("needs-you-dialog-approve")).toHaveCount(0);
  await expect(dialog.getByTestId("needs-you-text")).toHaveCount(0);
  await expect(dialog.getByTestId("needs-you-dismiss")).toBeDisabled();
  // A successful Retry re-establishes the evidence, and only then the decision.
  fail = false;
  await outdated.getByRole("button", { name: "Retry" }).click();
  await expect(dialog.getByTestId("needs-you-outdated")).toHaveCount(0);
  await expect(dialog.getByText("out of date", { exact: false })).toHaveCount(0);
  await expect(dialog.getByTestId("needs-you-dialog-approve")).toBeVisible();
  await expect(dialog.getByTestId("needs-you-dismiss")).toBeEnabled();
});

test("details render the last words' markdown, and keep the screen only for a menu (#1168, #1169)", async ({ page }) => {
  await mockShell(page);
  await mockNeedsYou(page, {
    lastWords: "Done.\n\n- **Checkouts:** under `~/agentwork/`.\n- **Next:** P5 <b>waits</b>.",
  });
  await page.goto("/dashboard");
  await needs(page).getByTestId("needs-you-row").nth(1).getByTestId("needs-you-details").click();
  const dialog = page.getByTestId("needs-you-dialog");
  const words = dialog.getByTestId("needs-you-last-words");
  await expect(words.locator("li")).toHaveCount(2);
  await expect(words.locator("strong").first()).toHaveText("Checkouts:");
  await expect(words.locator("code")).toHaveText("~/agentwork/");
  await expect(words).not.toContainText("**");
  await expect(words.locator("b")).toHaveCount(0); // agent HTML stays literal
  await expect(words).toContainText("<b>waits</b>");
  await expect(dialog.getByTestId("needs-you-screen")).toHaveCount(0);
  await page.keyboard.press("Escape");
  // A menu decision: what is being approved lives on screen, so it stays.
  await needs(page).getByTestId("needs-you-row").first().getByTestId("needs-you-details").click();
  await expect(dialog.getByTestId("needs-you-screen")).toContainText("Delete it");
});

test("the details dialog never scrolls as a whole: long last words scroll inside their block (#1170)", async ({ page }) => {
  await mockShell(page);
  const long = Array.from({ length: 80 }, (_, i) => `- **Step ${i}:** did a thing in \`file${i}.ts\`.`).join("\n");
  await mockNeedsYou(page, { lastWords: long });
  // A laptop-height window, like the report's: the menu row carries both evidence blocks.
  await page.setViewportSize({ width: page.viewportSize()!.width, height: 600 });
  for (const row of [0, 1]) {
    await page.goto("/dashboard");
    await needs(page).getByTestId("needs-you-row").nth(row).getByTestId("needs-you-details").click();
    const dialog = page.getByTestId("needs-you-dialog");
    await expect(dialog.getByTestId("needs-you-last-words")).toContainText("Step 79");
    const [scroll, client] = await dialog.evaluate((el) => [el.scrollHeight, el.clientHeight]);
    expect(scroll).toBeLessThanOrEqual(client);
    await expect(dialog.getByTestId("needs-you-dialog-approve")).toBeInViewport({ ratio: 1 });
    const words = dialog.getByTestId("needs-you-last-words");
    const inner = await words.evaluate((el) => el.scrollHeight > el.clientHeight);
    expect(inner).toBe(true);
  }
});
