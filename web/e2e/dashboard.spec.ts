/** The BattleLab dashboard (#1123 Phase 2): the tiles around Ask's sections, in a real browser on
 *  desktop and mobile, both themes. API mocked. What is pinned is what the issue asks for:
 *  every tile loads and fails ON ITS OWN (a failing tile never blanks the page), a failed read is
 *  never drawn as "0", the quota tile lists only quotas that were read (an unconfigured, stale or
 *  refused agent is left off) and says when a pace runs out, counts open exactly what they count, the phone keeps agent · project under each recent session, and asking opens
 *  the conversation on Ask's own page, from which the back arrow returns here (#1171). */
import { expect, test, type Page } from "@playwright/test";

import { ASK_STREAM, fulfillAsk } from "./askStream";

const NOW = Math.floor(Date.now() / 1000);
const A = "claude:aaaaaaaa-0000-4000-8000-00000000000a";
const B = "codex:aaaaaaaa-0000-4000-8000-00000000000b";

function liveRow(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    title: `session ${id.slice(-1)}`,
    engine: id.split(":")[0],
    project: { id: "p1", name: "Alpha" },
    working: true,
    last_activity: NOW - 60,
    state_line: "running the e2e suite",
    ...over,
  };
}

const DASH = {
  live: {
    health: "ok",
    total: 7,
    working: 3,
    by_engine: {
      claude: { live: 5, working: 2 },
      codex: { live: 2, working: 1 },
    },
    rows: [liveRow(A), liveRow(B, { working: false })],
  },
  recent: {
    total: 2,
    rows: [
      {
        id: A,
        title: "Newest session",
        engine: "claude",
        project: { id: "p1", name: "Alpha" },
        band: "in_flight",
        running: true,
        last_activity: NOW - 30,
      },
      {
        id: B,
        title: "Older session",
        engine: "codex",
        project: { id: "p2", name: "Beta" },
        band: "needs_you",
        running: false,
        last_activity: NOW - 3600,
      },
    ],
  },
};

function mission(id: string, state: string) {
  return {
    id,
    title: `mission ${id}`,
    project_id: null,
    cwd: null,
    state,
    created_at: NOW - 7200,
    updated_at: NOW - 120,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [A],
  };
}

const USAGE = {
  budgets: {},
  agents: [
    {
      engine: "claude",
      source: "plan",
      windows: [{ label: "weekly", used_pct: 82, resets_at: NOW + 86400 }],
      at: NOW - 400,
      checked_at: NOW - 400,
      stale: false,
      limit_tokens: 0,
      manual_used: 0,
      used_pct: 82,
      forecast: {
        state: "exhausts",
        window: "weekly",
        resets_at: NOW + 86400,
        runs_out_at: NOW + 9 * 3600,
        pct_at_reset: null,
        rate_per_h: 2,
      },
    },
    {
      engine: "codex",
      source: "plan",
      windows: [{ label: "weekly", used_pct: 40, resets_at: NOW + 86400 }],
      at: NOW - 12000,
      checked_at: NOW - 60,
      stale: true,
      error: "not logged in",
      limit_tokens: 0,
      manual_used: 0,
      used_pct: 40,
    },
    {
      engine: "opencode",
      source: "tokens",
      windows: [],
      tokens: { in: 1_000_000, out: 100_000 },
      at: NOW - 300,
      checked_at: NOW - 300,
      stale: false,
      limit_tokens: 5_000_000,
      manual_used: 0,
      used_pct: 99,
      forecast: {
        state: "ok",
        window: null,
        resets_at: null,
        runs_out_at: null,
        pct_at_reset: null,
        rate_per_h: 0,
      },
    },
    {
      engine: "kimi",
      source: "none",
      windows: [],
      at: 0,
      checked_at: null,
      stale: false,
      limit_tokens: 0,
      manual_used: 0,
      used_pct: null,
    },
  ],
};

type Handler = { status?: number; json: unknown };

async function mockAll(
  page: Page,
  over: {
    dash?: Handler;
    missions?: Handler;
    usage?: Handler;
    running?: Handler;
  } = {},
) {
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
  await page.route("**/api/sessions**", (r) => {
    const url = new URL(r.request().url());
    if (url.searchParams.get("running") === "live") {
      const h = over.running ?? {
        json: {
          sessions: Array.from({ length: 7 }, (_, i) => ({
            id: `claude:aaaaaaaa-0000-4000-8000-00000000001${i}`,
            title: `running ${i}`,
            engine: "claude",
            project: { kind: "project", id: "p1", name: "Alpha" },
            working: i < 3,
            last_mtime: NOW - i * 60,
          })),
          total: 7,
          next_offset: null,
          facets: { projects: [], engines: [] },
        },
      };
      return r.fulfill({ status: h.status ?? 200, json: h.json });
    }
    return r.fulfill({
      json: {
        sessions: [],
        total: 0,
        next_offset: null,
        facets: { projects: [], engines: [] },
      },
    });
  });
  await page.route("**/api/version", (r) =>
    r.fulfill({ json: { version: "test" } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({
      json: { notifications: [], unread: 0, uncertain: 0, settled: [] },
    }),
  );
  await page.route(/\/api\/pulse\/needs-you(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        rows: [],
        total: 0,
        total_unfiltered: 0,
        needs_you_ids: [],
        truncated: false,
        facets: { engines: [], projects: [] },
        window_days: 1,
      },
    }),
  );
  await page.route(/\/api\/pulse\/recap(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        window_days: 1,
        source: "local",
        generated_at: NOW,
        stale: false,
        configured: true,
        error: null,
        entries: [],
      },
    }),
  );
  await page.route(/\/api\/dashboard\/sessions/, (r) => {
    const h = over.dash ?? { json: DASH };
    return r.fulfill({ status: h.status ?? 200, json: h.json });
  });
  await page.route("**/api/missions**", (r) => {
    if (over.missions)
      return r.fulfill({
        status: over.missions.status ?? 200,
        json: over.missions.json,
      });
    const state = new URL(r.request().url()).searchParams.get("state");
    const rows =
      state === "running"
        ? [mission("m1", "running")]
        : state === "dispatching"
          ? [mission("m2", "dispatching")]
          : state === "review"
            ? [mission("m3", "review")]
            : [];
    return r.fulfill({
      json: {
        missions: rows,
        total: rows.length,
        limit: 5,
        offset: 0,
        facets: { projects: [], states: [] },
      },
    });
  });
  await page.route("**/api/agents/usage", (r) => {
    const h = over.usage ?? { json: USAGE };
    return r.fulfill({ status: h.status ?? 200, json: h.json });
  });
}

test("every tile renders its own read, and the strip counts what the tiles show", async ({
  page,
}) => {
  await mockAll(page);
  await page.goto("/dashboard");
  await expect(
    page.getByRole("heading", { name: "BattleLab dashboard" }),
  ).toBeVisible();
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
  await expect(page.getByTestId("kpi-agents")).toContainText("3 working");
  await expect(page.getByTestId("kpi-agents")).toContainText("claude 5/2");
  await expect(page.getByTestId("kpi-missions")).toContainText(
    "2 active · 1 in review",
  );
  await expect(
    page.getByTestId("dash-running").getByTestId("running-row"),
  ).toHaveCount(2);
  await expect(
    page.getByTestId("dash-missions").getByTestId("mission-row"),
  ).toHaveCount(3);
  // Latest by last activity, in the order the server sorted them.
  const recent = page.getByTestId("recent-session-row");
  await expect(recent.first()).toContainText("Newest session");
  await expect(recent.nth(1)).toContainText("Needs you");
});

test("plan quota: the strip names the LOWEST plan window; tokens/manual never compete", async ({
  page,
}) => {
  await mockAll(page);
  await page.goto("/dashboard");
  // claude has 18 % left (82 used); opencode's 99 % used is a token count, not a plan quota.
  await expect(page.getByTestId("kpi-quota")).toContainText("18%");
  await expect(page.getByTestId("kpi-quota")).toContainText("claude");
});

test("quota: an agent that is not configured, stale or refused is left off the tile", async ({
  page,
}) => {
  // kimi is `none` (never configured), codex's figures went stale behind "not logged in", and
  // gemini's vendor refuses the account: none of them has a quota that was read.
  await mockAll(page, {
    usage: {
      json: {
        ...USAGE,
        agents: [
          ...USAGE.agents,
          {
            engine: "gemini",
            source: "plan",
            windows: [{ label: "day", used_pct: 10, resets_at: NOW + 3600 }],
            at: NOW - 60,
            checked_at: NOW - 60,
            stale: false,
            limit_tokens: 0,
            manual_used: 0,
            used_pct: 10,
            access: {
              state: "denied",
              message: "This client is no longer supported",
              observed_at: NOW - 60,
              checked_at: NOW - 60,
            },
          },
        ],
      },
    },
  });
  await page.goto("/dashboard");
  const rows = page.getByTestId("quota-row");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toHaveAttribute("data-engine", "claude");
  await expect(rows.nth(1)).toHaveAttribute("data-engine", "opencode");
  await expect(page.getByTestId("dash-quota")).not.toContainText(
    "not measured",
  );
  await expect(page.getByTestId("dash-quota")).not.toContainText("no access");
});

test("quota: nothing readable says so, and points at where a limit is set", async ({
  page,
}) => {
  await mockAll(page, {
    usage: {
      json: {
        ...USAGE,
        agents: USAGE.agents.filter((a) => a.source === "none"),
      },
    },
  });
  await page.goto("/dashboard");
  await expect(page.getByTestId("quota-row")).toHaveCount(0);
  await expect(page.getByTestId("dash-quota")).toContainText(
    "No agent’s quota can be read right now",
  );
});

for (const theme of ["dark", "light"] as const) {
  test(`quota: the forecast says when a pace runs out, in the degraded colour — ${theme}`, async ({
    page,
  }) => {
    await mockAll(page);
    await page.goto("/dashboard");
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, theme);
    const claude = page
      .locator('[data-testid="quota-row"][data-engine="claude"]')
      .getByTestId("quota-forecast");
    await expect(claude).toContainText(/runs out in ~9h \(.+\) at this pace/);
    await expect(claude).toContainText("15h before the reset");
    const colour = await claude.evaluate((el) => getComputedStyle(el).color);
    const degraded = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--status-degraded)";
      document.body.append(probe);
      const c = getComputedStyle(probe).color;
      probe.remove();
      return c;
    });
    expect(colour).toBe(degraded);
    // The line sits inside its row, never clipped by it.
    const row = (await page
      .locator('[data-testid="quota-row"][data-engine="claude"]')
      .boundingBox())!;
    const line = (await claude.boundingBox())!;
    expect(line.x + line.width).toBeLessThanOrEqual(row.x + row.width + 1);
    expect(line.y + line.height).toBeLessThanOrEqual(row.y + row.height + 1);
    await expect(
      page
        .locator('[data-testid="quota-row"][data-engine="opencode"]')
        .getByTestId("quota-forecast"),
    ).toHaveText("on pace to stay under the limit");
  });
}

test("a failing tile never blanks the page, and says it could not read — never '0'", async ({
  page,
}) => {
  await mockAll(page, { dash: { status: 500, json: { detail: "boom" } } });
  await page.goto("/dashboard");
  await expect(
    page.getByTestId("dash-running").getByTestId("tile-error"),
  ).toBeVisible();
  await expect(page.getByTestId("kpi-agents")).toContainText("couldn’t read");
  await expect(page.getByTestId("kpi-agents")).not.toContainText("0 live");
  // The others still stand.
  await expect(
    page.getByTestId("dash-missions").getByTestId("mission-row"),
  ).toHaveCount(3);
  await expect(page.getByTestId("quota-row")).toHaveCount(2);
});

test("an unreadable runtime is 'couldn't read', never '0 running'", async ({
  page,
}) => {
  await mockAll(page, {
    dash: { json: { ...DASH, live: { health: "unavailable" } } },
  });
  await page.goto("/dashboard");
  await expect(page.getByTestId("running-unavailable")).toBeVisible();
  await expect(page.getByTestId("running-empty")).toHaveCount(0);
  await expect(page.getByTestId("kpi-agents")).toContainText("couldn’t read");
});

test("a mission store that won't answer is an error, never 'no missions'", async ({
  page,
}) => {
  await mockAll(page, {
    missions: {
      json: {
        missions: [],
        total: 0,
        limit: 5,
        offset: 0,
        facets: { projects: [], states: [] },
        store_error: "locked",
      },
    },
  });
  await page.goto("/dashboard");
  await expect(
    page.getByTestId("dash-missions").getByTestId("tile-error"),
  ).toBeVisible();
  await expect(page.getByTestId("missions-empty")).toHaveCount(0);
});

test("'All N running' opens exactly the N the count said", async ({ page }) => {
  await mockAll(page);
  await page.goto("/dashboard");
  await page.getByTestId("running-show-all").click();
  await expect(
    page.getByTestId("dash-running").getByTestId("running-row"),
  ).toHaveCount(7);
});

test("a count is a way to what it counts: the agents number opens the running tile", async ({
  page,
}) => {
  await mockAll(page);
  await page.goto("/dashboard");
  await page.getByTestId("kpi-agents").click();
  await expect(page.getByTestId("dash-running")).toBeInViewport();
});

test("asking opens the conversation on Ask's page; the back arrow returns to the tiles (#1171)", async ({
  page,
}) => {
  await mockAll(page);
  await page.route(ASK_STREAM, (r) =>
    fulfillAsk(r, {
      answer: "Two sessions ran.",
      matches: [],
      mission_matches: [],
    }),
  );
  await page.goto("/dashboard");
  await page.getByTestId("composer-input").fill("what ran today?");
  await page.getByTestId("composer-send").click();
  await expect(page).toHaveURL(/\/ask$/);
  await expect(page.getByTestId("ask-turn")).toHaveCount(1);
  // The conversation carries none of the tiles (the operator's call, #1171)…
  await expect(page.getByTestId("dash-running")).toHaveCount(0);
  await expect(page.getByTestId("dash-kpis")).toHaveCount(0);
  // …and one tap brings them back.
  await page.getByRole("link", { name: "Back to dashboard" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId("kpi-agents")).toContainText("7");
});

test("on a phone, a recent session keeps agent · project under its title", async ({
  page,
}, info) => {
  test.skip(info.project.name !== "mobile", "phone layout");
  await mockAll(page);
  await page.goto("/dashboard");
  const row = page.getByTestId("recent-session-row").first();
  await expect(
    row.locator("td").first().getByText("claude · Alpha"),
  ).toBeVisible();
});

for (const theme of ["dark", "light"] as const) {
  test(`renders in the ${theme} theme`, async ({ page }, info) => {
    await mockAll(page);
    await page.goto("/dashboard");
    await page.evaluate((t) => {
      document.documentElement.dataset.theme = t;
    }, theme);
    await expect(page.getByTestId("dash-kpis")).toBeVisible();
    await expect(page.getByTestId("quota-row")).toHaveCount(2);
    await page.screenshot({
      path: `test-results/dashboard-${info.project.name}-${theme}.png`,
      fullPage: true,
    });
  });
}

test("long agent-authored titles and project names never overflow the page sideways", async ({
  page,
}) => {
  const long = "x".repeat(400);
  await mockAll(page, {
    dash: {
      json: {
        ...DASH,
        live: {
          ...DASH.live,
          rows: [liveRow(A, { title: long, state_line: long })],
        },
        recent: {
          total: 1,
          rows: [
            {
              ...DASH.recent.rows[0],
              title: long,
              project: { id: "p", name: long },
            },
          ],
        },
      },
    },
  });
  await page.goto("/dashboard");
  await expect(page.getByTestId("recent-session-row")).toHaveCount(1);
  const pane = page.getByTestId("dashboard-pane");
  const [scroll, client] = await pane.evaluate((el) => [
    el.scrollWidth,
    el.clientWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(client + 1);
});
