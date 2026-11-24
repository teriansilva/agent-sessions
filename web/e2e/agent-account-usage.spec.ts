import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { agentPath } from "../src/routes/settingsTabs";

// Every quota of every account (#2331), in a real browser: an agent's Usage section shows every
// window of the account its NEW sessions start on, with that account's name and email; the
// Accounts section shows each account's email and windows, the Default login's included; and
// picking another account switches the Usage section with no probe. The network is mocked; the
// mock answers `/api/agents/usage` from the pick the page saved, as the server does.

const FIXTURE = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/roster.fixture.json"), "utf8"),
) as { engines: { id: string; label: string; capabilities: unknown }[] };
const CLAUDE = FIXTURE.engines.find((e) => e.id === "claude")!;
const NOW = Math.floor(Date.now() / 1000);
const DETAIL = {
  id: CLAUDE.id,
  label: CLAUDE.label,
  publisher: "battlelab",
  version: "1",
  contract: 1,
  source: "in-tree",
  kind: "agent",
  runtime: "pty",
  binary: { name: CLAUDE.id, env_var: "AGENT_SESSIONS_CLAUDE_BIN", search_paths: [] },
  provenance: { state: "adopted", via: "search_paths", path: "/usr/local/bin/claude", note: null },
  store: { root: "~/.claude", resolved: "/home/operator/.claude", layout: "claude-projects", read_only: true },
  launch: { resume: "flag", new: "pin-flag", admission: null },
  transcript: { kind: "claude-jsonl", strict: false },
  usage: { source: "plan", kind: "claude-cli-probe" },
  capabilities: CLAUDE.capabilities,
  models: [],
  maintenance: [],
};

function row(id: string, label: string, email: string | null, pcts: [number, number, number], plan: string) {
  const [session, week, fable] = pcts;
  return {
    engine: CLAUDE.id,
    account: { id, label, email },
    source: "plan",
    windows: [
      { label: "session", used_pct: session, resets_at: NOW + 3 * 3600 },
      { label: "week (all models)", used_pct: week, resets_at: NOW + 2 * 86400 },
      { label: "week (Fable)", used_pct: fable, resets_at: NOW + 2 * 86400 },
    ],
    plan,
    at: NOW - 240,
    checked_at: NOW - 240,
    stale: false,
    error: null,
    limit_tokens: 0,
    manual_used: 0,
    used_pct: Math.max(session, week, fable),
    forecast: null,
  };
}

const ROWS = {
  default: row("default", "Default login", "op@work.example", [3, 100, 58], "max_5x"),
  "a-11111111": row("a-11111111", "Personal", "op@personal.example", [12, 91, 42], "max_20x"),
};

async function mock(page: Page) {
  const state = {
    engine: CLAUDE.id,
    new_sessions: "default",
    max_accounts: 5,
    accounts: [{ id: "a-11111111", label: "Personal", created_at: 1, problems: [] as string[] }],
  };
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [CLAUDE.id],
        terminal_backend: "ws",
        auth_mode: "none",
        agent_defaults: { default_engine: CLAUDE.id, bypass: true },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) => r.fulfill({ json: { engines: FIXTURE.engines, problems: [] } }));
  await page.route(/\/api\/engines\/[^/]+$/, (r) => r.fulfill({ json: DETAIL }));
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) => r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }));
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({ json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } } }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  const probes: string[] = [];
  await page.route("**/api/agents/usage**", (r) => {
    if (r.request().method() !== "GET") probes.push(r.request().url());
    const active = state.new_sessions as keyof typeof ROWS;
    const ids = ["default", ...state.accounts.map((a) => a.id)] as (keyof typeof ROWS)[];
    return r.fulfill({
      json: {
        budgets: { threshold_pct: 90, notify: true, engines: {} },
        agents: [ROWS[active]],
        accounts: { [CLAUDE.id]: ids.map((id) => ({ ...ROWS[id], active: id === active })) },
      },
    });
  });
  await page.route(/\/api\/agent-accounts(\/.*)?$/, (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    if (path === "/api/agent-accounts") return r.fulfill({ json: { agents: [state] } });
    if (path.endsWith("/new-sessions")) {
      state.new_sessions = (req.postDataJSON() as { account: string }).account;
      return r.fulfill({ json: state });
    }
    if (req.method() === "DELETE") {
      // As the server does: forgetting the picked account moves new sessions to the default login.
      const id = path.split("/").pop();
      state.accounts = state.accounts.filter((a) => a.id !== id);
      if (state.new_sessions === id) state.new_sessions = "default";
      return r.fulfill({ json: state });
    }
    return r.fulfill({ json: state });
  });
  return probes;
}

const usageSection = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "Usage", exact: true }) });
const accountsSection = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "Accounts", exact: true }) });

test("Usage lists every window of the account new sessions start on, and follows the pick", async ({ page }) => {
  const probes = await mock(page);
  await page.goto(agentPath(CLAUDE.id));
  const usage = usageSection(page);
  const head = usage.getByTestId("usage-account");
  await expect(head).toContainText("Default login");
  await expect(head).toContainText("op@work.example");
  await expect(head).toContainText("max_5x plan");
  await expect(head).toContainText("New sessions start here");
  const windows = usage.locator("[data-window]");
  await expect(windows).toHaveCount(3);
  await expect(usage.locator('[data-window="week (all models)"]')).toContainText("100%");
  await expect(usage.locator('[data-window="week (all models)"]')).toHaveAttribute("data-first", "true");
  await expect(usage.locator('[data-window="week (Fable)"]')).toContainText("58%");

  // Accounts: the Default login's email and its windows; every account's, compactly.
  const accounts = accountsSection(page);
  const def = accounts.getByTestId("account-default");
  await expect(def.getByTestId("account-email")).toContainText("op@work.example");
  await expect(def.locator("[data-window]")).toHaveCount(3);
  await expect(accounts.getByRole("radio", { name: /Default login · op@work\.example/ })).toBeChecked();
  const personal = accounts.getByTestId("account-a-11111111");
  await expect(personal.getByTestId("account-email")).toContainText("op@personal.example");
  await expect(personal.locator('[data-window="week (all models)"]')).toContainText("91%");

  // Pick Personal: the Usage section is now Personal's, without anyone asking a vendor.
  await accounts.getByRole("radio", { name: /Personal/ }).check();
  await expect(head).toContainText("Personal");
  await expect(head).toContainText("op@personal.example");
  await expect(usage.locator('[data-window="week (all models)"]')).toContainText("91%");
  await expect(usage.locator('[data-window="session"]')).toContainText("12%");
  expect(probes).toEqual([]);

  const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(sideways).toBeLessThanOrEqual(1);
});

test("removing the account new sessions start on switches Usage back to the default login (Hermes on #2340)", async ({ page }) => {
  const probes = await mock(page);
  await page.goto(agentPath(CLAUDE.id));
  const usage = usageSection(page);
  const head = usage.getByTestId("usage-account");
  const accounts = accountsSection(page);
  await accounts.getByRole("radio", { name: /Personal/ }).check();
  await expect(head).toContainText("op@personal.example");
  await expect(usage.locator('[data-window="week (all models)"]')).toContainText("91%");

  const row = accounts.getByTestId("account-a-11111111");
  await row.getByRole("button", { name: "Remove" }).click();
  await row.getByRole("button", { name: "Forget account" }).click();
  await expect(accounts.getByTestId("account-a-11111111")).toHaveCount(0);
  // The server moved new sessions to the default login: the header and the meters follow at once.
  // With no account left there is no pool to name a choice in, so the login's email stands alone.
  await expect(head).toHaveText("op@work.example · max_5x plan");
  await expect(usage.locator('[data-window="week (all models)"]')).toContainText("100%");
  await expect(accounts.getByRole("radio", { name: /Default login/ })).toBeChecked();
  expect(probes).toEqual([]);
});

test("at 320px every window still fits: label, bar and figure, nothing pushed sideways", async ({ page }) => {
  await mock(page);
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto(agentPath(CLAUDE.id));
  const usage = usageSection(page);
  await expect(usage.locator("[data-window]")).toHaveCount(3);
  const bad = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    return [...document.querySelectorAll("[data-window], [data-testid=usage-account]")]
      .map((n) => n.getBoundingClientRect().right)
      .filter((r) => r > vw + 1);
  });
  expect(bad).toEqual([]);
  const meter = usage.locator('[data-window="session"] [role=meter]');
  expect((await meter.boundingBox())!.width).toBeGreaterThan(100);
});
