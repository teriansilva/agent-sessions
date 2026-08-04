import { expect, test } from "@playwright/test";

// Pulse "Ask" (#522): the natural-language session finder embedded at the top of /pulse.
// Real-browser proof (mobile + desktop, per agent-workflow §5), red on pre-#522 builds:
// type a question → the answer line + the matched session card render → "Jump in" routes
// to the session view; and a LONG question wraps inside the thread with no horizontal
// scroll (the #494 rule extends to the Ask panel).

const CONFIG = {
  csrf: "x",
  new_session_engines: ["claude"],
  terminal_backend: "ws",
  auth_mode: "none",
  overview_expanded: [],
  projects_hidden: [],
  pulse: {
    auto_enabled: false,
    interval_minutes: 30,
    window_days: 3,
    scan_depth: "fast",
    configured: true,
  },
};

const T = 1_700_000_000;

const EMPTY_OVERVIEW = {
  cache_version: 2,
  generated_at: T - 60,
  window_days: 3,
  scan_depth: "fast",
  input_fingerprint: "fp",
  synthesis_skipped: false,
  banner: null,
  cards: [],
};

const MATCH = {
  id: "claude:1b2f3a4c-0000-4000-8000-abcdefabcdef",
  engine: "claude",
  title: "fix ws delta-resume reconnect",
  cwd: "/home/u/agent-sessions",
  project: {
    kind: "folder",
    id: "/home/u/agent-sessions",
    name: "agent-sessions",
  },
  last_activity: T - 7200,
  ai_summary: "Reconnect backoff fixed; tests green",
  intervention_required: false,
  intervention_reason: "",
  reviewed_at: T - 7200,
  live: false,
  state: "idle",
  synthesis: null,
  why: "transcript discusses reconnect backoff and delta-resume",
};

// A genuinely long, unbroken token in the question — must wrap, never scroll.
const LONG_Q = `where did I debug the ${"reconnect_backoff_delta_resume_".repeat(6)} handshake?`;

async function stubApp(page: import("@playwright/test").Page) {
  await page.route("**/api/config", (r) => r.fulfill({ json: CONFIG }));
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
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/pulse", (r) => r.fulfill({ json: EMPTY_OVERVIEW }));
}

test("Ask answers with a matched card and Jump in routes to the session (#522)", async ({
  page,
}) => {
  await stubApp(page);
  await page.route("**/api/pulse/ask", async (r) => {
    const body = r.request().postDataJSON() as {
      query: string;
      history: unknown[];
    };
    expect(body.query).toContain("websocket reconnect");
    await r.fulfill({
      json: {
        answer:
          "That was your ws delta-resume session in agent-sessions, 2 hours ago.",
        matches: [MATCH],
        stage: "content",
        configured: true,
      },
    });
  });

  await page.goto("/pulse");
  const input = page.getByRole("textbox", {
    name: /ask about your past work/i,
  });
  await input.fill(
    "I worked on the websocket reconnect bug — which session was that?",
  );
  await page.getByRole("button", { name: /^ask$/i }).click();

  // Answer line + the matched session card (the reused Pulse card) render in the thread.
  await expect(
    page.getByText(/that was your ws delta-resume session/i),
  ).toBeVisible();
  await expect(page.getByText("fix ws delta-resume reconnect")).toBeVisible();
  await expect(
    page.getByText(/transcript discusses reconnect backoff/i),
  ).toBeVisible();

  // Jump in navigates into the session view (the card id's engine/uuid route).
  await page
    .getByRole("link", { name: /jump into fix ws delta-resume reconnect/i })
    .click();
  await expect(page).toHaveURL(
    /\/s\/claude\/1b2f3a4c-0000-4000-8000-abcdefabcdef/,
  );
});

test("a long question + a matched card fit at 320px — no horizontal scroll (#522/#494)", async ({
  page,
}) => {
  // 320px is the worst case Hermes flagged on PR #547: the Ask panel's padding leaves
  // ~266px of content width, under the card grid's desktop 280px min track — a matched
  // card (not just an answer line) must shrink, never clip or scroll sideways.
  await page.setViewportSize({ width: 320, height: 780 });
  await stubApp(page);
  await page.route("**/api/pulse/ask", (r) =>
    r.fulfill({
      json: {
        answer: "That long-token session is this one.",
        matches: [
          {
            ...MATCH,
            title: `debug ${"reconnect_backoff_delta_resume_".repeat(3)}handshake`,
            why: `transcript token ${"reconnect_backoff_delta_resume_".repeat(3)} matches`,
          },
        ],
        stage: "content",
        configured: true,
      },
    }),
  );

  await page.goto("/pulse");
  const input = page.getByRole("textbox", {
    name: /ask about your past work/i,
  });
  await input.fill(LONG_Q);
  await page.getByRole("button", { name: /^ask$/i }).click();
  await expect(
    page.getByText(/that long-token session is this one/i),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /jump into/i })).toBeVisible();

  // No element on the page is user-scrollable horizontally (same probe as #494).
  const overflowers = await page.evaluate(() => {
    const bad: string[] = [];
    for (const el of Array.from(document.querySelectorAll("*"))) {
      const cs = getComputedStyle(el);
      const scrollable = ["auto", "scroll"].includes(cs.overflowX);
      if (scrollable && el.scrollWidth > el.clientWidth + 1) {
        bad.push(`${el.tagName}.${(el as HTMLElement).className}`);
      }
    }
    return bad;
  });
  expect(overflowers).toEqual([]);
});
