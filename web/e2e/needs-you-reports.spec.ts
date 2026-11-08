/** An agent's own report in NEEDS YOU, and its progress line on the session row (#1882).
 *
 *  Real browser, API mocked, desktop (1280) and phone (390), dark and light. What is pinned is what
 *  the Commander relies on: the report is framed as the agent's own words; an option tap posts the
 *  option's NUMBER for that report and re-reads the list; Dismiss posts the report and sends
 *  nothing; with orchestration off the options are disabled with the reason and Open session goes
 *  to the session; every control is ≥ 44 px; long reports and long option labels stay inside the
 *  row; the options run two columns on a desktop and one on a phone; focus moves title → options
 *  → Dismiss → details. */
import { expect, test, type Page, type Request } from "@playwright/test";

import { NEW_SESSION_PATH } from "../src/lib/routes";
import { mockAskConversations } from "./askStream";
import { TOUCH_FLOOR } from "./touchTarget";

const A = "claude:aaaaaaaa-0000-4000-8000-00000000000a";
const B = "codex:aaaaaaaa-0000-4000-8000-00000000000b";
const NOW = Math.floor(Date.now() / 1000);
const REASON =
  "Answering from here needs the orchestrator on (Settings → Mission control). Open the session to answer it.";
const LONG = "an unusually long report sentence that keeps going ".repeat(5).trim();
const LONG_OPTION = "Keep the signed cookie and shorten its life to one hour unless".slice(0, 80);

function rows(available = true, long = false) {
  return [
    {
      id: A,
      engine: "claude",
      title: "Move session tokens off the cookie",
      project: { id: "p1", name: "auth-service" },
      last_activity: NOW - 60,
      since: NOW - 180,
      reason: "",
      summary: "",
      flagged: false,
      kind: "report_decision",
      menu: null,
      action: null,
      report: {
        id: "rep-a",
        state: "needs_decision",
        summary: long
          ? LONG
          : "Both token stores work and pass the suite. Choosing changes the migration, so I stopped here.",
        question: "Where should session tokens live?",
        options: [
          "Postgres — the table we already back up",
          "Redis — faster, but one more service to run",
          long ? LONG_OPTION : "Keep the signed cookie, shorten its life to 1 h",
          "Stop here; I'll read the branch myself",
        ],
        at: NOW - 180,
      },
      report_answer: available ? { available: true, reason: "" } : { available: false, reason: REASON },
    },
    {
      id: B,
      engine: "codex",
      title: "Fix the nightly e2e job",
      project: { id: "p2", name: "web" },
      last_activity: NOW - 600,
      since: NOW - 660,
      reason: "",
      summary: "",
      flagged: false,
      kind: "report_blocked",
      menu: null,
      action: null,
      report: {
        id: "rep-b",
        state: "blocked",
        summary: "CI fails at the same step three runs in a row: the runner gets a 403 pulling the browser image.",
        at: NOW - 660,
      },
      report_answer: { available: true, reason: "" },
    },
  ];
}

async function mockShell(page: Page, theme: string, list: () => unknown) {
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
        theme,
        hq_reports: true,
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: {
        sessions: [
          {
            id: A,
            engine: "claude",
            uuid: A.split(":")[1],
            short_uuid: "aaaaaaaa",
            cwd: "/work/billing",
            project: { kind: "folder", id: "/work/billing", name: "billing" },
            last_mtime: NOW - 60,
            first_user_message: "",
            title: "Migrate billing exports",
            sticky: false,
            archived: false,
            agent_report: { state: "progress", summary: "3 of 4 packages green; fixing the last", at: NOW - 120 },
          },
        ],
        total: 1,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  await page.route(/\/api\/pulse\/recap(\?.*)?$/, (r) =>
    r.fulfill({
      json: { window_days: 1, source: "ai", generated_at: NOW, stale: false, configured: true, error: null, entries: [] },
    }),
  );
  await mockAskConversations(page);
  const seen = { lists: 0, answers: [] as Request[], dismisses: [] as Request[] };
  await page.route(/\/api\/pulse\/needs-you(\?.*)?$/, (r) => {
    seen.lists += 1;
    const data = list() as unknown[];
    return r.fulfill({
      json: {
        rows: data,
        total: data.length,
        total_unfiltered: data.length,
        needs_you_ids: (data as { id: string }[]).map((x) => x.id),
        truncated: false,
        facets: { engines: ["claude", "codex"], projects: [] },
        window_days: 1,
      },
    });
  });
  await page.route(/\/api\/needs-you\/.+\/report\/answer$/, (r) => {
    seen.answers.push(r.request());
    return r.fulfill({ json: { id: "act", state: "delivered" } });
  });
  await page.route(/\/api\/needs-you\/.+\/report\/dismiss$/, (r) => {
    seen.dismisses.push(r.request());
    return r.fulfill({ json: { dismissed: true } });
  });
  return seen;
}

async function open(page: Page, isPhone: boolean) {
  if (isPhone) await page.setViewportSize({ width: 390, height: 844 });
  else await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/dashboard");
}

const needs = (page: Page) => page.getByTestId("needs-you");

for (const theme of ["dark", "light"]) {
  test(`the report reads as the agent's own words, with numbered options (${theme})`, async ({ page }, info) => {
    const phone = info.project.name === "mobile";
    await mockShell(page, theme, () => rows(true, true));
    await open(page, phone);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    const row = needs(page).getByTestId("needs-you-row").first();
    await expect(row.getByText("The agent asks you to decide")).toBeVisible();
    await expect(row.getByText(/its own words, unchecked/)).toBeVisible();
    await expect(row.getByTestId("needs-you-report-summary")).toHaveText(LONG);
    await expect(row.getByTestId("needs-you-report-question")).toHaveText("Where should session tokens live?");
    const opts = row.getByTestId("needs-you-report-option");
    await expect(opts).toHaveCount(4);
    await expect(opts.nth(2)).toContainText(LONG_OPTION);
    // Desktop: two columns. Phone: one.
    const b0 = (await opts.nth(0).boundingBox())!;
    const b1 = (await opts.nth(1).boundingBox())!;
    if (phone) expect(b1.y).toBeGreaterThan(b0.y + b0.height - 1);
    else expect(Math.abs(b1.y - b0.y)).toBeLessThan(2);
    // Long words stay inside the row, and the page never scrolls sideways.
    const rowBox = (await row.boundingBox())!;
    for (const o of await opts.all()) {
      const b = (await o.boundingBox())!;
      expect(b.x).toBeGreaterThanOrEqual(rowBox.x - 0.5);
      expect(b.x + b.width).toBeLessThanOrEqual(rowBox.x + rowBox.width + 0.5);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    // The blocked variant: words and Dismiss, no options.
    const blocked = needs(page).getByTestId("needs-you-row").nth(1);
    await expect(blocked.getByText("The agent says it's blocked")).toBeVisible();
    await expect(blocked.getByTestId("needs-you-report-option")).toHaveCount(0);
    // ≥ 44 px on every control in the section (§8).
    for (const b of await needs(page).getByRole("button").all()) {
      expect((await b.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    }
  });
}

test("an option tap posts that report and the option's number, and re-reads the list", async ({ page }, info) => {
  const seen = await mockShell(page, "dark", () => rows());
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").first();
  await expect(row.getByTestId("needs-you-report-option")).toHaveCount(4);
  const before = seen.lists;
  await row.getByTestId("needs-you-report-option").nth(1).click();
  await expect.poll(() => seen.answers.length).toBe(1);
  expect(new URL(seen.answers[0].url()).pathname).toBe(`/api/needs-you/${encodeURIComponent(A)}/report/answer`);
  expect(seen.answers[0].postDataJSON()).toEqual({ report_id: "rep-a", option: 2 });
  await expect.poll(() => seen.lists).toBeGreaterThan(before);
  expect(seen.dismisses).toHaveLength(0);
});

for (const [code, said, notSaid] of [
  ["stale", /^Nothing was sent/, /may already have been sent/],
  ["spent", /may already have been sent/, /Nothing was sent/],
] as const) {
  test(`a ${code} refusal says ${code === "stale" ? "nothing was sent" : "it may already have been sent"}`, async ({ page }, info) => {
    await mockShell(page, "dark", () => rows());
    const detail =
      code === "stale"
        ? "the agent's report was replaced, answered or dismissed"
        : "an answer to this report was already sent, or may have been";
    await page.route(/\/api\/needs-you\/.+\/report\/answer$/, (r) =>
      r.fulfill({ status: 409, json: { detail, code } }),
    );
    await open(page, info.project.name === "mobile");
    const row = needs(page).getByTestId("needs-you-row").first();
    await row.getByTestId("needs-you-report-option").first().click();
    const alert = row.getByTestId("needs-you-row-error");
    await expect(alert).toHaveText(said);
    await expect(alert).not.toHaveText(notSaid);
    const link = alert.getByRole("link", { name: "Open session" });
    await expect(link).toBeVisible();
    await link.click();
    await expect(page).toHaveURL(/\/s\/claude\/aaaaaaaa-0000-4000-8000-00000000000a$/);
  });
}

test("an uncertain outcome says it may already have been sent, never success", async ({ page }, info) => {
  await mockShell(page, "dark", () => rows());
  await page.route(/\/api\/needs-you\/.+\/report\/answer$/, (r) =>
    r.fulfill({ json: { id: "act", state: "indeterminate", outcome: "uncertain" } }),
  );
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").first();
  await row.getByTestId("needs-you-report-option").first().click();
  const alert = row.getByTestId("needs-you-row-error");
  await expect(alert).toHaveText(/may already have been sent/);
  await expect(alert).not.toHaveText(/Nothing was sent/);
  await expect(alert.getByRole("link", { name: "Open session" })).toBeVisible();
});

test("a lost response on the answer POST says it may have been sent and keeps the options locked", async ({ page }, info) => {
  await mockShell(page, "dark", () => rows());
  await page.route(/\/api\/needs-you\/.+\/report\/answer$/, (r) => r.abort("connectionreset"));
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").first();
  await row.getByTestId("needs-you-report-option").first().click();
  const alert = row.getByTestId("needs-you-row-error");
  await expect(alert).toHaveText(/may already have been sent/);
  await expect(alert).not.toHaveText(/Nothing was sent/);
  await expect(alert.getByRole("link", { name: "Open session" })).toBeVisible();
  for (const o of await row.getByTestId("needs-you-report-option").all()) await expect(o).toBeDisabled();
});

test("Dismiss posts the report and sends nothing", async ({ page }, info) => {
  const seen = await mockShell(page, "light", () => rows());
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").nth(1);
  // It ENDS the report, and says so — not the screen "hide" Details offers (#1882).
  await expect(row.getByTestId("needs-you-report-dismiss")).toHaveText("Dismiss report");
  expect((await row.getByTestId("needs-you-report-dismiss").boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await row.getByTestId("needs-you-report-dismiss").click();
  await expect.poll(() => seen.dismisses.length).toBe(1);
  expect(seen.dismisses[0].postDataJSON()).toEqual({ report_id: "rep-b" });
  expect(seen.answers).toHaveLength(0);
});

test("with orchestration off the options are disabled with the reason, and Open session works", async ({ page }, info) => {
  const seen = await mockShell(page, "dark", () => rows(false));
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").first();
  const opts = row.getByTestId("needs-you-report-option");
  await expect(opts).toHaveCount(4);
  for (const o of await opts.all()) await expect(o).toBeDisabled();
  await opts.first().click({ force: true });
  const reason = row.getByTestId("needs-you-report-reason");
  await expect(reason).toContainText(REASON);
  const link = reason.getByRole("link", { name: "Open session" });
  expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  expect(seen.answers).toHaveLength(0);
  await link.click();
  await expect(page).toHaveURL(/\/s\/claude\/aaaaaaaa-0000-4000-8000-00000000000a$/);
});

test("focus moves title → options → Dismiss → details", async ({ page }, info) => {
  await mockShell(page, "dark", () => rows());
  await open(page, info.project.name === "mobile");
  const row = needs(page).getByTestId("needs-you-row").first();
  await row.getByRole("link", { name: "Move session tokens off the cookie" }).focus();
  const order: string[] = [];
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    order.push(await page.evaluate(() => (document.activeElement as HTMLElement).dataset.testid ?? ""));
  }
  expect(order).toEqual([
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-option",
    "needs-you-report-dismiss",
    "needs-you-details",
  ]);
});

test("the session row shows the agent's progress line, framed as agent says", async ({ page }, info) => {
  await mockShell(page, "dark", () => []);
  // The sessions sidebar: a grid column on a desktop, the drawer behind the toggle on a phone.
  await page.setViewportSize(
    info.project.name === "mobile" ? { width: 390, height: 844 } : { width: 1280, height: 900 },
  );
  await page.goto(NEW_SESSION_PATH);
  if (info.project.name === "mobile") await page.locator("header .navToggle").click();
  await expect(page.locator("aside.sidebar")).toBeVisible();
  const note = page.getByTestId("row-agent-note");
  await expect(note).toBeVisible();
  await expect(note).toContainText("Agent says");
  await expect(note).toContainText("3 of 4 packages green; fixing the last");
  // One line: never taller than a line of its own type.
  expect((await note.boundingBox())!.height).toBeLessThan(24);
});
