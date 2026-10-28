/** API first (#1728 Phase 1) in a real browser, desktop and phone, both themes.
 *
 *  The server's `families` block decides each family's default client; the New session form
 *  preselects it, marks it, keeps the operator's explicit pick per family on this device, and says
 *  with the create whether its client is the automatic default or an explicit choice. What only a
 *  browser shows: the marks and the remembered row are on screen, "Use API" is a 44 px target, the
 *  terminal is one tap away, a stale default never navigates anywhere, and the launch's socket
 *  carries the provenance. */
import { expect, test, type Page } from "@playwright/test";
import { mockRoster } from "./roster";
import { agentPicker, projectPicker } from "./newSessionForm";
import { NEW_SESSION_PATH } from "../src/lib/routes";
import { TOUCH_FLOOR } from "./touchTarget";

const FAMILY_KEY = "battlelab.familyChoice.v1";

const PROJECT = {
  id: "p-1",
  name: "agent-sessions",
  color: "#ffb000",
  folders: ["/home/u/claude/agent-sessions"],
  default_folder: "/home/u/claude/agent-sessions",
  archived: false,
  created_at: 0,
};

function families(opts: { claudeApiReady?: boolean } = {}) {
  const ready = opts.claudeApiReady ?? true;
  return [
    {
      family: "claude",
      clients: ["claude", "claude-api"],
      interactive_default: ready
        ? { engine: "claude-api", reason: "api_ready" }
        : { engine: "claude", reason: "api_unready" },
      unattended_admitted: [],
      reasons: ready ? {} : { "claude-api": "claude 2.1.287 or later is required" },
    },
    {
      family: "codex",
      clients: ["codex", "codex-api"],
      interactive_default: { engine: "codex", reason: "api_unready" },
      unattended_admitted: [],
      reasons: { "codex-api": "codex app-server 0.164 not admitted" },
    },
    {
      family: "opencode",
      clients: ["opencode", "opencode-api"],
      interactive_default: { engine: "opencode-api", reason: "api_ready" },
      unattended_admitted: [],
      reasons: {},
    },
  ];
}

async function mockBackend(
  page: Page,
  opts: { claudeApiReady?: boolean; createStatus?: number } = {},
) {
  const ready = opts.claudeApiReady ?? true;
  const creates: Record<string, unknown>[] = [];
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: [
          "claude",
          ...(ready ? ["claude-api"] : []),
          "codex",
          "opencode",
          "opencode-api",
        ],
        unavailable_clients: [
          ...(ready
            ? []
            : [{ id: "claude-api", label: "Claude — API", reason: "claude 2.1.287 or later is required" }]),
          { id: "codex-api", label: "Codex — API", reason: "codex app-server 0.164 not admitted" },
        ],
        terminal_backend: "ws",
        auth_mode: "none",
        default_project_id: "p-1",
        agent_defaults: { default_engine: "claude", bypass: false },
      },
    }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [PROJECT] } }));
  await page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  await page.route("**/api/structured/**", async (r) => {
    const req = r.request();
    if (req.method() === "POST" && new URL(req.url()).pathname === "/api/structured/sessions") {
      creates.push(req.postDataJSON() as Record<string, unknown>);
      if (opts.createStatus === 409)
        return r.fulfill({
          status: 409,
          json: {
            detail: "the default changed to Claude Code since this was shown — nothing was started",
            default: { family: "claude", engine: "claude", reason: "api_unready" },
          },
        });
      return r.fulfill({ status: 201, json: { session_key: "claude-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } });
    }
    return r.fulfill({ json: { status: "ok", reason: null, models: [] } });
  });
  await mockRoster(page, { families: families({ claudeApiReady: ready }) });
  return creates;
}

const tile = (page: Page, id: string) => page.locator(`label[data-engine="${id}"]`);

test("a ready API client is preselected and marked default; Terminal is one tap away (#1728)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  const agent = agentPicker(page);
  await expect(projectPicker(page).radio("p-1")).toBeChecked();
  await expect(agent.radio("claude-api")).toBeChecked();
  await expect(tile(page, "claude-api").getByTestId("new-session-family-mark")).toHaveText("default");
  // Another family whose API client cannot start: its terminal is the default THERE, said why.
  await expect(tile(page, "codex")).toContainText("default here");
  await expect(tile(page, "codex")).toContainText("used because the API client is unavailable");
  await expect(page.getByTestId("new-session-unavailable").filter({ hasText: "Codex — API" })).toContainText(
    "0.164 not admitted",
  );
  // One tap to the terminal, and every tile is a 44 px target.
  for (const id of ["claude", "claude-api"]) {
    expect((await tile(page, id).boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
  await agent.selectOption("claude");
  await expect(agent.radio("claude")).toBeChecked();
});

test("an automatic API default is created as `automatic` (#1728)", async ({ page }) => {
  const creates = await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  await expect(agentPicker(page).radio("claude-api")).toBeChecked();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/claude-api\//);
  expect(creates).toHaveLength(1);
  expect(creates[0]).toMatchObject({ engine: "claude-api", choice: "automatic" });
});

test("a stale automatic default is refused: the form stays, and says so (#1728)", async ({ page }) => {
  const creates = await mockBackend(page, { createStatus: 409 });
  await page.goto(NEW_SESSION_PATH);
  await expect(agentPicker(page).radio("claude-api")).toBeChecked();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page.getByTestId("start-error")).toContainText("the default changed to Claude Code");
  await expect(page.getByTestId("start-error")).toContainText("press Start again");
  await expect(page).toHaveURL(new RegExp(`${NEW_SESSION_PATH}$`)); // nothing opened
  expect(creates).toHaveLength(1);
});

test("an explicit Terminal pick is remembered on this device and launches as `explicit` (#1728)", async ({ page }) => {
  await mockBackend(page);
  await page.goto(NEW_SESSION_PATH);
  const agent = agentPicker(page);
  await expect(agent.radio("claude-api")).toBeChecked();
  await agent.selectOption("claude");
  const socket = page.waitForEvent("websocket", (ws) => ws.url().includes("/ws/term/claude:"));
  await page.getByRole("button", { name: /start session/i }).click();
  const url = new URL((await socket).url().replace(/^ws/, "http"));
  expect(url.searchParams.get("new")).toBe("1");
  expect(url.searchParams.get("choice")).toBe("explicit");
  expect(await page.evaluate((k) => localStorage.getItem(k), FAMILY_KEY)).toContain('"claude":"claude"');

  // Back to the form: the remembered Terminal pick wins, says so, and "Use API" is one tap away.
  await page.goto(NEW_SESSION_PATH);
  await expect(agent.radio("claude")).toBeChecked();
  await expect(tile(page, "claude").getByTestId("new-session-family-mark")).toHaveText("remembered");
  const hint = page.getByTestId("new-session-remembered");
  await expect(hint).toContainText("you last chose Claude Code for claude");
  const useApi = hint.getByRole("button", { name: "Use API" });
  expect((await useApi.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  await useApi.click();
  await expect(agent.radio("claude-api")).toBeChecked();
  await expect(page.getByTestId("new-session-remembered")).toHaveCount(0);
});

test("a Terminal pick made while the API client is unready still wins once it is ready (#1728)", async ({ page }) => {
  await mockBackend(page, { claudeApiReady: false });
  await page.goto(NEW_SESSION_PATH);
  const agent = agentPicker(page);
  await expect(agent.radio("claude")).toBeChecked(); // the default HERE: the API client is unready
  await expect(tile(page, "claude")).toContainText("default here");
  await agent.selectOption("claude"); // …and the operator picks it explicitly anyway
  const socket = page.waitForEvent("websocket", (ws) => ws.url().includes("/ws/term/claude:"));
  await page.getByRole("button", { name: /start session/i }).click();
  expect(new URL((await socket).url().replace(/^ws/, "http")).searchParams.get("choice")).toBe(
    "explicit",
  );

  // The API client is ready now: the explicit Terminal pick still overrides the API preselection.
  await mockBackend(page, { claudeApiReady: true });
  await page.goto(NEW_SESSION_PATH);
  await expect(agent.radio("claude")).toBeChecked();
  await expect(tile(page, "claude").getByTestId("new-session-family-mark")).toHaveText("remembered");
  await expect(page.getByTestId("new-session-remembered")).toContainText("Claude Code");
});

test("the API client becomes ready between display and Start: the form stays, explains, and Start retries (#1728)", async ({ page }) => {
  const sockets: string[] = [];
  await page.routeWebSocket(/\/ws\/term\//, (ws) => {
    sockets.push(ws.url());
  });
  await mockBackend(page, { claudeApiReady: false });
  await page.goto(NEW_SESSION_PATH);
  const agent = agentPicker(page);
  await expect(agent.radio("claude")).toBeChecked(); // the terminal is the default HERE
  const creates = await mockBackend(page, { claudeApiReady: true }); // …and the API client is now ready
  await page.getByRole("button", { name: /start session/i }).click();
  const err = page.getByTestId("start-error");
  await expect(err).toContainText("The default for claude changed to Claude — API");
  await expect(err).toContainText("press Start again");
  await expect(page).toHaveURL(new RegExp(`${NEW_SESSION_PATH}$`)); // the form stayed
  expect(sockets).toEqual([]); // no terminal was opened
  await expect(agent.radio("claude-api")).toBeChecked(); // the new default, on screen
  await page.getByRole("button", { name: /start session/i }).click(); // the deliberate retry
  await expect(page).toHaveURL(/\/s\/claude-api\//);
  expect(creates[0]).toMatchObject({ engine: "claude-api", choice: "automatic" });
});

test("a terminal launch the socket refuses as a stale default (4412) comes back to the form (#1728)", async ({ page }) => {
  const reason = "the default changed to Claude — API since this was shown — nothing was started";
  await page.routeWebSocket(/\/ws\/term\//, (ws) => ws.close({ code: 4412, reason }));
  await mockBackend(page, { claudeApiReady: false });
  await page.goto(NEW_SESSION_PATH);
  await expect(agentPicker(page).radio("claude")).toBeChecked();
  await page.getByRole("button", { name: /start session/i }).click();
  const panel = page.locator('[data-session-opening="stale-default"]');
  await expect(panel).toContainText(reason);
  const back = panel.getByTestId("stale-default-back");
  expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  const creates = await mockBackend(page, { claudeApiReady: true });
  await back.click();
  await expect(page).toHaveURL(new RegExp(`${NEW_SESSION_PATH}$`));
  await expect(page.getByTestId("start-error")).toContainText(reason);
  await expect(agentPicker(page).radio("claude-api")).toBeChecked();
  await page.getByRole("button", { name: /start session/i }).click();
  await expect(page).toHaveURL(/\/s\/claude-api\//);
  expect(creates[0]).toMatchObject({ engine: "claude-api" });
});

test("a remembered client that cannot start is ignored, with the reason (#1728)", async ({ page }) => {
  await page.addInitScript(
    (k) => localStorage.setItem(k, JSON.stringify({ v: 1, choices: { claude: "claude-api" } })),
    FAMILY_KEY,
  );
  await mockBackend(page, { claudeApiReady: false });
  await page.goto(NEW_SESSION_PATH);
  await expect(agentPicker(page).radio("claude")).toBeChecked();
  const stale = page.getByTestId("new-session-remembered-stale");
  await expect(stale).toContainText("Claude — API");
  await expect(stale).toContainText("2.1.287 or later is required");
  await expect(tile(page, "claude")).toContainText("default here");
});

for (const theme of ["dark", "light"] as const) {
  test(`captures the API-first picker — ${theme} (#1728)`, async ({ page }, info) => {
    await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
    await mockBackend(page);
    await page.goto(NEW_SESSION_PATH);
    await expect(agentPicker(page).radio("claude-api")).toBeChecked();
    // Nothing in the agent section overflows the viewport sideways, in either theme.
    const group = (await page.getByRole("radiogroup", { name: "Agent" }).boundingBox())!;
    expect(group.x + group.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
    await page.screenshot({ path: info.outputPath(`api-first-${theme}.png`), fullPage: true });
    await agentPicker(page).selectOption("claude");
    await page.evaluate(
      (k) => localStorage.setItem(k, JSON.stringify({ v: 1, choices: { claude: "claude" } })),
      FAMILY_KEY,
    );
    await page.reload();
    await expect(page.getByTestId("new-session-remembered")).toBeVisible();
    await page.screenshot({ path: info.outputPath(`api-first-${theme}-remembered.png`), fullPage: true });
  });
}
