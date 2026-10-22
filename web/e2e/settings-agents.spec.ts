import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Locator, type Page, test } from "@playwright/test";
import {
  agentPath,
  settingsPath,
} from "../src/routes/settingsTabs";

// Settings → AGENTS (#853 P4, #1128) in a real browser, because what breaks here is layout: a
// roster grid or a long binary path that pushes a phone sideways, a radio row under 44px, a
// defaults form the keyboard cannot reach. jsdom has no layout, so none of that is visible to the
// unit tests. The network is mocked from the manifest-generated roster; nothing needs a backend.

interface Engine {
  id: string;
  label: string;
  present: boolean;
  bin: string | null;
  status?: string;
  status_reason?: string | null;
  display: Record<string, unknown>;
  [k: string]: unknown;
}

const FIXTURE = JSON.parse(
  readFileSync(resolve(process.cwd(), "src/test/roster.fixture.json"), "utf8"),
) as { engines: Engine[] };

/** A binary path with no break opportunity for a long way — it must WRAP, never widen the card. */
const LONG_BIN =
  "/home/operator/.local/share/toolchains/node/lib/node_modules/@openai/codex/bin/codex-cli-launcher-with-a-deliberately-long-name.js";

/** The default agent the operator stored, made absent below: the removed-default state. */
const STORED_DEFAULT = "gemini";

const ENGINES: Engine[] = [
  ...FIXTURE.engines.map((e) =>
    e.id === "codex"
      ? { ...e, bin: LONG_BIN }
      : e.id === STORED_DEFAULT
        ? { ...e, present: false, bin: null }
        : e,
  ),
  {
    ...FIXTURE.engines.find((e) => e.id === "kimi")!,
    id: "legacy-cli",
    label: "legacy-cli",
    bin: "/home/operator/.local/bin/legacy-cli",
    display: { name: "legacy-cli", badge: "lc", accent: "slate", id_prefix: null, order: 95 },
    status: "retiring",
    status_reason: "agent removed — its running sessions stay attachable until they exit.",
  },
];

const PROBLEMS = [
  {
    source: "plugins/first_party/acme-agent/plugin.toml",
    error: "binary.name: must be the plugin id or one of binary.aliases",
  },
];

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
  binary: {
    name: CLAUDE.id,
    env_var: "AGENT_SESSIONS_CLAUDE_BIN",
    search_paths: ["~/.local/bin"],
  },
  provenance: { state: "adopted", via: "search_paths", path: LONG_BIN, note: null },
  store: { root: "~/.claude", resolved: "/home/operator/.claude", layout: "claude-projects", read_only: true },
  launch: { resume: "flag", new: "pin-flag", admission: null },
  transcript: { kind: "claude-jsonl", strict: false },
  usage: { source: "plan", kind: "claude-cli-probe" },
  capabilities: CLAUDE.capabilities,
  models: [],
  maintenance: [],
};

async function mockAgents(page: Page, posts: unknown[] = []) {
  await page.route("**/api/**", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ENGINES.map((e) => e.id),
        terminal_backend: "ws",
        auth_mode: "none",
        agent_defaults: { default_engine: STORED_DEFAULT, bypass: true },
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/engines", (r) =>
    r.fulfill({ json: { engines: ENGINES, problems: PROBLEMS } }),
  );
  await page.route(/\/api\/engines\/[^/]+$/, (r) =>
    r.request().url().endsWith(`/api/engines/${CLAUDE.id}`)
      ? r.fulfill({ json: DETAIL })
      : r.fulfill({ status: 404, json: { detail: "unknown engine" } }),
  );
  await page.route("**/api/agents/usage", (r) =>
    r.fulfill({
      json: {
        budgets: { threshold_pct: 90, notify: true, engines: {} },
        agents: [
          {
            engine: CLAUDE.id,
            source: "plan",
            at: 1,
            checked_at: 1,
            stale: false,
            limit_tokens: 0,
            manual_used: 0,
            used_pct: 62,
            windows: [{ label: "5h", used_pct: 62, resets_at: null }],
          },
        ],
      },
    }),
  );
  await page.route("**/api/prefs", (r) => {
    if (r.request().method() === "POST") posts.push(r.request().postDataJSON());
    return r.fulfill({ json: { agent_defaults: { default_engine: STORED_DEFAULT, bypass: true } } });
  });
  await page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [] } }));
  await page.route(/\/api\/projects($|\?)/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse$/, (r) =>
    r.fulfill({ json: { cards: [], generated_at: 1, window_days: 3 } }),
  );
  await page.route("**/api/pulse/notifications", (r) =>
    r.fulfill({ json: { notifications: [], unread: 0, uncertain: 0, settled: [] } }),
  );
  await page.route("**/api/missions**", (r) =>
    r.fulfill({
      json: { missions: [], total: 0, next_offset: null, facets: { projects: [], states: [] } },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } },
    }),
  );
}

/** Every visible element inside `root` ends inside the viewport. `scrollWidth` alone misses this:
 *  the settings page scrolls on its OWN box, so content can overflow it with a clean document. */
async function assertNoHorizontalOverflow(root: Locator, label: string) {
  const worst = await root.evaluate((el) => {
    const vw = document.documentElement.clientWidth;
    let bad: { tag: string; text: string; right: number } | null = null;
    for (const n of [el, ...el.querySelectorAll("*")]) {
      const r = n.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (n.closest(".sr-only")) continue;
      if (r.right > vw + 1 && (!bad || r.right > bad.right)) {
        bad = { tag: n.tagName, text: (n.textContent ?? "").slice(0, 40), right: r.right };
      }
    }
    return { vw, bad, doc: document.documentElement.scrollWidth - window.innerWidth };
  });
  expect(worst.bad, `${label}: ${JSON.stringify(worst.bad)} past ${worst.vw}px`).toBeNull();
  expect(worst.doc, `${label}: document scrolls sideways`).toBeLessThanOrEqual(1);
}

async function assertTargets(targets: Locator, min = 44) {
  const n = await targets.count();
  expect(n).toBeGreaterThan(0);
  for (let i = 0; i < n; i++) {
    const t = targets.nth(i);
    if (!(await t.isVisible())) continue;
    await t.scrollIntoViewIfNeeded();
    const box = (await t.boundingBox())!;
    expect(box.height, `${await t.textContent()} height`).toBeGreaterThanOrEqual(min);
  }
}

const WIDTHS = [320, 390, 1440];
const rosterSection = (page: Page) =>
  page.locator("section", { has: page.getByRole("heading", { name: "Agents", exact: true }) });

test("roster: every state side by side, 44px targets, no overflow at 320–1440 (#1408)", async ({
  page,
}) => {
  // Three widths, and every target scrolled into view and measured at each: 14–18 s on an idle
  // host, past the 30 s default on a loaded CI runner (it timed out in scrollIntoViewIfNeeded on
  // #1564, desktop and mobile, with nothing in that PR touching this page). Its own budget, as
  // #1159 gave the overview workspace spec.
  test.setTimeout(90_000);
  await mockAgents(page);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(settingsPath("agents"));
    const section = rosterSection(page);
    const list = section.getByRole("list", { name: "All agents" });
    await expect(list.getByRole("link", { name: "Details for Claude Code" })).toBeVisible();
    // Each state is ONE word beside an LED (design.md §8), from the one presenter.
    await expect(list.getByText("Invalid", { exact: true })).toBeVisible();
    await expect(list.getByText("Retiring", { exact: true })).toBeVisible();
    await expect(list.getByText("Not installed", { exact: true }).first()).toBeVisible();
    await expect(section.getByTestId("roster-summary")).toContainText(/\d+ ready · \d+ need attention/);
    // Rows are compact: one per CLI, never the old card wall.
    for (const row of await list.locator(":scope > li").all()) {
      const box = (await row.boundingBox())!;
      expect(box.height, `row height @${width}`).toBeLessThanOrEqual(width < 800 ? 260 : 90);
    }
    // Add agent stays on the header at every width.
    await assertTargets(section.getByRole("link", { name: "Add agent", exact: true }));
    await assertTargets(list.getByRole("link", { name: /^Details for / }));
    await assertTargets(list.locator("[data-mode]"));
    await assertTargets(section.getByRole("group", { name: "Agent filters" }).getByRole("button"));
    await assertTargets(section.getByRole("link", { name: "Defaults →" }));
    await assertNoHorizontalOverflow(section, `roster @${width}`);
  }
});

test("agent page: status first, the long path wraps, technical detail folded away, and a 404 says so", async ({
  page,
}) => {
  await mockAgents(page);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(agentPath(CLAUDE.id));
    // The header: the resolved binary (a long one WRAPS, never widens the page) and the ⋯ menu.
    const path = page.getByText(LONG_BIN, { exact: true }).first();
    await expect(path).toBeVisible();
    expect((await path.boundingBox())!.height, `path wraps at ${width}px`).toBeGreaterThan(18);
    await assertTargets(page.getByTestId("agent-menu"));
    // Technical detail is folded away until asked for.
    const tech = page.getByTestId("technical-details");
    await expect(page.getByRole("heading", { name: "Binary & provenance" })).toBeHidden();
    await tech.locator("summary").click();
    await expect(page.getByRole("heading", { name: "Binary & provenance" })).toBeVisible();
    await expect(tech.getByText("None declared")).toBeVisible();
    await assertTargets(tech.locator("summary"));
    await assertNoHorizontalOverflow(page.locator("main, body").first(), `agent page @${width}`);
  }
  await page.goto(agentPath("not-an-agent"));
  await expect(page.getByRole("heading", { name: "No such agent" })).toBeVisible();
  await assertTargets(page.getByRole("link", { name: "← All agents" }));
});

test("defaults: the removed default is named, the keyboard reaches the radios and Save, and a save sends only what changed", async ({
  page,
}) => {
  const posts: unknown[] = [];
  await mockAgents(page, posts);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(settingsPath("agents-defaults"));
    const notice = page.getByRole("status").filter({ hasText: "Your default" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(
      "Your default, Gemini CLI, is not installed — new sessions use Claude Code until it is. Your choice is kept.",
    );
    const section = page.locator("section", {
      has: page.getByRole("heading", { name: "Defaults", exact: true }),
    });
    await assertTargets(section.locator("label"));
    await assertNoHorizontalOverflow(section, `defaults @${width}`);
  }

  // Keyboard: from the All agents link, Tab reaches the agent radios, then the switch; arrowing
  // to another agent makes the form dirty and Save becomes the next stop.
  const roster = page.getByRole("link", { name: "← All agents" });
  await roster.focus();
  const stops: string[] = [];
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press("Tab");
    const at = await page.evaluate(() => {
      const el = document.activeElement as HTMLInputElement;
      return `${el.tagName}:${el.type ?? ""}:${el.getAttribute("role") ?? ""}:${el.value ?? ""}`;
    });
    stops.push(at);
    if (at.startsWith("INPUT:radio")) break;
  }
  expect(stops.at(-1), stops.join(" → ")).toMatch(/^INPUT:radio/);
  const radioFocus = await page.evaluate(() => {
    const cs = getComputedStyle(
      (document.activeElement as HTMLElement).closest("label")!,
    );
    return { style: cs.outlineStyle, width: parseFloat(cs.outlineWidth) };
  });
  expect(radioFocus.style).toBe("solid");
  expect(radioFocus.width).toBeGreaterThanOrEqual(2);

  await page.keyboard.press("ArrowDown");
  const picked = await page.evaluate(
    () => (document.activeElement as HTMLInputElement).value,
  );
  expect(picked).not.toBe("");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("switch", { name: /permission bypass/i })).toBeFocused();
  await page.keyboard.press("Tab");
  const save = page.getByRole("button", { name: "Save defaults" });
  await expect(save).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(posts).toEqual([{ agent_defaults: { default_engine: picked } }]);
});

test("defaults: saving the bypass alone never re-sends the uninstalled default", async ({
  page,
}) => {
  const posts: unknown[] = [];
  await mockAgents(page, posts);
  await page.goto(settingsPath("agents-defaults"));
  await page.getByRole("switch", { name: /permission bypass/i }).click();
  await page.getByRole("button", { name: "Save defaults" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(posts).toEqual([{ agent_defaults: { bypass: false } }]);
});

for (const theme of ["dark", "light"] as const) {
  test(`roster (#1408): one row per CLI links each mode; Needs attention narrows; Add agent at 320px · ${theme}`, async ({
    page,
  }, info) => {
    await mockAgents(page);
    await page.goto(settingsPath("agents"));
    await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
    const list = page.getByRole("list", { name: "All agents" });
    // Codex and its API client are ONE row; each mode chip opens its own agent page.
    const codex = list.locator('[data-engine="codex"]');
    const apis = ENGINES.filter((e) => e.runtime === "api" && (e.api as { source?: string } | undefined)?.source === "codex");
    await expect(codex.locator("[data-mode]")).toHaveCount(1 + apis.length);
    await expect(codex.locator('[data-mode="codex"]')).toHaveAttribute("href", agentPath("codex"));
    for (const a of apis) {
      await expect(list.locator(`[data-engine="${a.id}"]`)).toHaveCount(0);
      await expect(codex.locator(`[data-mode="${a.id}"]`)).toHaveAttribute("href", agentPath(a.id));
    }
    await page.screenshot({ path: info.outputPath(`roster-${theme}.png`), fullPage: true });
    // Needs attention shows only rows that are not Ready.
    await page.getByRole("button", { name: "Needs attention" }).click();
    for (const word of await list.locator("[data-status]").evaluateAll((n) => n.map((x) => x.getAttribute("data-status")))) {
      expect(word).not.toBe("Ready");
    }
    await page.getByRole("button", { name: "All", exact: true }).click();
    // Add agent stays in the header at the narrowest width.
    await page.setViewportSize({ width: 320, height: 800 });
    await assertTargets(page.getByRole("link", { name: "Add agent", exact: true }));
    await assertNoHorizontalOverflow(rosterSection(page), "roster @320");
    await page.screenshot({ path: info.outputPath(`roster-320-${theme}.png`), fullPage: true });
    // The agent page: header, sections, technical details closed.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(agentPath(CLAUDE.id));
    await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
    await expect(page.getByTestId("technical-details")).not.toHaveAttribute("open", "");
    await page.screenshot({ path: info.outputPath(`agent-${theme}.png`), fullPage: true });
    await page.getByTestId("agent-menu").click();
    await page.screenshot({ path: info.outputPath(`agent-menu-${theme}.png`) });
  });
}

test("roster (#1408): at 320px every toolbar control stays inside the roster box", async ({ page }) => {
  await mockAgents(page);
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(settingsPath("agents"));
  const box = (await page.getByRole("list", { name: "All agents" }).boundingBox())!;
  for (const control of [page.getByLabel("Search agents"), ...(await page.getByRole("group", { name: "Agent filters" }).getByRole("button").all())]) {
    const b = (await control.boundingBox())!;
    expect(b.x + b.width, "inside the roster's right edge").toBeLessThanOrEqual(box.x + box.width + 1);
    expect(b.height, "a control stays control-sized when the toolbar stacks").toBeLessThanOrEqual(90);
  }
});
