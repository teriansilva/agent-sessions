import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Locator, type Page, test } from "@playwright/test";
import { agentPath } from "../src/routes/settingsTabs";
import { TOUCH_FLOOR } from "./touchTarget";

// Settings → Agents → agent → Accounts (#1820 P1b) in a real browser: the section appears only for
// an agent the server lists, the new-session pick and the account actions reach their routes, the
// sign-in terminal attaches to the account's own socket, and nothing pushes a phone sideways or
// shrinks a target under 44px. The network is mocked; no backend and no vendor CLI.

const FIXTURE = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/roster.fixture.json"), "utf8"),
) as { engines: { id: string; label: string; capabilities: unknown }[] };
const CLAUDE = FIXTURE.engines.find((e) => e.id === "claude")!;
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

/** A label with no break opportunity: it must wrap inside its row, never widen the page. */
const LONG = "Max-subscription-for-the-weekend-team";

interface Pool {
  engine: string;
  new_sessions: string;
  max_accounts: number;
  accounts: { id: string; label: string; created_at: number; problems: string[] }[];
}

function pool(): Pool {
  return {
    engine: CLAUDE.id,
    new_sessions: "default",
    max_accounts: 5,
    accounts: [
      { id: "a-11111111", label: "Max 2", created_at: 1, problems: [] },
      { id: "a-22222222", label: LONG, created_at: 2, problems: ["sessions: a separate copy exists in this account's home"] },
    ],
  };
}

interface Calls {
  method: string;
  path: string;
  body: unknown;
}

async function mock(page: Page, listed: boolean) {
  const state = pool();
  const calls: Calls[] = [];
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
  // The app shell's own reads, as `settings-agents.spec.ts` mocks them.
  await page.route("**/api/agents/usage", (r) =>
    r.fulfill({ json: { budgets: { threshold_pct: 90, notify: true, engines: {} }, agents: [] } }),
  );
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
  await page.route(/\/api\/agent-accounts(\/.*)?$/, (r) => {
    const req = r.request();
    const path = new URL(req.url()).pathname;
    const body = req.postData() ? req.postDataJSON() : null;
    if (req.method() !== "GET") calls.push({ method: req.method(), path, body });
    if (path === "/api/agent-accounts") return r.fulfill({ json: { agents: listed ? [state] : [] } });
    if (path.endsWith("/new-sessions")) {
      state.new_sessions = (body as { account: string }).account;
      return r.fulfill({ json: state });
    }
    if (path.endsWith("/status")) return r.fulfill({ json: { signed_in: false } });
    if (path.endsWith("/signin/cancel")) return r.fulfill({ json: { closed: true } });
    if (req.method() === "POST" && path === `/api/agent-accounts/${CLAUDE.id}`) {
      const added = { id: "a-33333333", label: (body as { label: string }).label, created_at: 3, problems: [] };
      state.accounts.push(added);
      return r.fulfill({ json: added });
    }
    if (req.method() === "DELETE") {
      const id = path.split("/").pop();
      state.accounts = state.accounts.filter((a) => a.id !== id);
      return r.fulfill({ json: state });
    }
    return r.fulfill({ json: state });
  });
  return calls;
}

const section = (page: Page) => page.locator("section", { has: page.getByRole("heading", { name: "Accounts", exact: true }) });

async function assertNoHorizontalOverflow(root: Locator, label: string) {
  const worst = await root.evaluate((el) => {
    const vw = document.documentElement.clientWidth;
    let bad: { tag: string; text: string; right: number } | null = null;
    for (const n of [el, ...el.querySelectorAll("*")]) {
      const r = n.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > vw + 1 && (!bad || r.right > bad.right)) bad = { tag: n.tagName, text: (n.textContent ?? "").slice(0, 40), right: r.right };
    }
    return { vw, bad, doc: document.documentElement.scrollWidth - window.innerWidth };
  });
  expect(worst.bad, `${label}: ${JSON.stringify(worst.bad)} past ${worst.vw}px`).toBeNull();
  expect(worst.doc, `${label}: document scrolls sideways`).toBeLessThanOrEqual(1);
}

async function assertTargets(targets: Locator) {
  const n = await targets.count();
  expect(n).toBeGreaterThan(0);
  for (let i = 0; i < n; i++) {
    const t = targets.nth(i);
    if (!(await t.isVisible())) continue;
    await t.scrollIntoViewIfNeeded();
    const box = (await t.boundingBox())!;
    expect(box.height, `${await t.textContent()} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

test("an agent the server does not list has no Accounts section", async ({ page }) => {
  await mock(page, false);
  await page.goto(agentPath(CLAUDE.id));
  await expect(page.getByRole("heading", { name: "Usage", exact: true })).toBeVisible();
  await expect(section(page)).toHaveCount(0);
});

test("the pool: default and accounts, a broken account cannot be picked, no overflow and 44px targets at 320–1440, both themes", async ({ page }) => {
  await mock(page, true);
  // Every width in dark; light at the phone width (layout does not depend on the theme).
  for (const [scheme, widths] of [["dark", [320, 390, 1440]], ["light", [390]]] as const) {
    // The theme is the app's own choice (`tr-theme`), not the device's; init scripts run in
    // order, so the latest one decides.
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), scheme);
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(agentPath(CLAUDE.id));
      const s = section(page);
      await expect(s.getByRole("radio", { name: "Default login" })).toBeChecked();
      await expect(s.getByRole("radio", { name: "Max 2" })).toBeEnabled();
      await expect(s.getByRole("radio", { name: LONG })).toBeDisabled();
      await expect(s.getByTestId("account-a-22222222").getByText("Cannot be used")).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
      await assertNoHorizontalOverflow(s, `${scheme} ${width}px`);
      await assertTargets(s.getByRole("button"));
    }
  }
});

test("choosing the new-session account saves it; check asks the CLI; remove asks first", async ({ page }) => {
  const calls = await mock(page, true);
  await page.goto(agentPath(CLAUDE.id));
  const s = section(page);
  await s.getByRole("radio", { name: "Max 2" }).check();
  await expect.poll(() => calls.find((c) => c.path.endsWith("/new-sessions"))?.body).toEqual({ account: "a-11111111" });
  await expect(s.getByRole("radio", { name: "Max 2" })).toBeChecked();

  const row = s.getByTestId("account-a-11111111");
  await row.getByRole("button", { name: "Check" }).click();
  await expect(row.getByText("Needs sign-in")).toBeVisible();

  await row.getByRole("button", { name: "Remove" }).click();
  // The warning is the server's contract (Hermes on #2074): a re-added name is a new account.
  await expect(row.getByText(/will no longer open, and this cannot be undone here/)).toBeVisible();
  await expect(row.getByText(/does not bring them back/)).toBeVisible();
  expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  await row.getByRole("button", { name: "Forget account" }).click();
  await expect(s.getByTestId("account-a-11111111")).toHaveCount(0);
  expect(calls.filter((c) => c.method === "DELETE").map((c) => c.path)).toEqual([`/api/agent-accounts/${CLAUDE.id}/a-11111111`]);
});

test("adding an account opens the vendor's sign-in on that account's socket; closing it cancels", async ({ page }) => {
  const calls = await mock(page, true);
  const sockets: string[] = [];
  await page.routeWebSocket(/\/ws\/agent-accounts\//, (ws) => {
    sockets.push(new URL(ws.url()).pathname);
    ws.send(Buffer.from("Visit https://claude.ai/oauth/authorize?code=demo to sign in\r\nPaste code here if prompted > "));
  });
  await page.goto(agentPath(CLAUDE.id));
  const s = section(page);
  await s.getByLabel("New account name").fill("Max 3");
  await s.getByRole("button", { name: "Add account" }).click();
  expect(calls.find((c) => c.method === "POST" && c.path === `/api/agent-accounts/${CLAUDE.id}`)?.body).toEqual({ label: "Max 3" });
  const row = s.getByTestId("account-a-33333333");
  await expect(row.getByLabel("Temporary vendor sign-in terminal")).toBeVisible();
  await expect.poll(() => sockets).toEqual([`/ws/agent-accounts/${CLAUDE.id}/a-33333333/signin`]);
  await expect(row.getByRole("link", { name: /Open the sign-in link · claude\.ai/ })).toBeVisible();
  await row.getByRole("button", { name: "Close sign-in" }).click();
  await expect(row.getByLabel("Temporary vendor sign-in terminal")).toHaveCount(0);
  await expect.poll(() => calls.some((c) => c.path === `/api/agent-accounts/${CLAUDE.id}/a-33333333/signin/cancel`)).toBe(true);
  await assertNoHorizontalOverflow(s, "after sign-in");
});
