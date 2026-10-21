import { expect, type Page } from "@playwright/test";
import { mockRoster } from "./roster";

export const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const TURN = "11111111-2222-4333-8444-555555555555";
type Json = Record<string, unknown>;

export async function mockLivePermissions(page: Page, theme: "dark" | "light", engine = "codex-api", uncertain = false) {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "x", new_session_engines: [engine], unavailable_clients: [], terminal_backend: "ws",
    auth_mode: "none", default_project: "/home/u/proj", overview_expanded: [], projects_hidden: [], theme,
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page);
  const attempts: Json[] = [];
  let state: Json = {
    session_key: `${engine}:${ID}`, revision: 4, event_cursor: 4, cwd: "/home/u/proj",
    state: "awaiting_approval", active_turn: TURN, model_requested: null, model_effective: null,
    bypass: false, can_enable_bypass: true, permission_change: {}, omitted_turns: 0, read_only: null,
    native: { native_id: "n1", worker: "w1", background_active: false },
    turns: [{ turn_id: TURN, operation_id: TURN, state: "awaiting_approval", text: "Review the project",
      reply: "I will check the project guidelines.", tools: [], reason: null }],
    pending_requests: [{ request_id: "7", turn_id: TURN, kind: "command", complete: true,
      payload_digest: "abc", choices: ["approve", "reject", "cancel"],
      payload: { command: "cat shared/agent-workflow.md", cwd: "/home/u/proj" },
      always: [{ id: "g123", scope: "session", label: "Allow this command for the rest of this session" }],
    }],
  };
  await page.route(new RegExp(`/api/structured/sessions/${engine}(?::|%3A)${ID}(/[-a-z]+)?(\\?.*)?$`), async (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path.endsWith("/skip-permissions")) {
      const body = r.request().postDataJSON() as Json;
      attempts.push(body);
      if (uncertain && attempts.length === 1) {
        state = { ...state, permission_change: { state: "stopping", operation_id: body.operation_id } };
        return r.fulfill({ status: 503, json: { detail: "Session tools may still be running." } });
      }
      state = { ...state, bypass: true, can_enable_bypass: false, state: "idle", active_turn: null,
        permission_change: { state: "enabled", operation_id: body.operation_id }, pending_requests: [],
        turns: (state.turns as Json[]).map((turn) => ({ ...turn, state: "uncertain",
          reason: "the native worker ended before this turn settled" })),
        native: { native_id: "n1", worker: null, background_active: false } };
      return r.fulfill({ json: state });
    }
    if (path.endsWith("/events")) return r.fulfill({ json: { revision: 4, next_cursor: 4, events: [] } });
    return r.fulfill({ json: state });
  });
  return attempts;
}

export async function openPermissions(page: Page) {
  const pane = page.getByTestId("structured-pane");
  let attempt = 0;
  await expect(async () => {
    if (attempt++ > 0) await page.keyboard.press("Escape");
    const direct = pane.getByRole("button", { name: "Enable Skip permissions for this session" });
    if (await direct.isVisible()) return direct.click({ timeout: 2000 });
    const menu = pane.getByTestId("head-actions-menu");
    if (await menu.isVisible()) await menu.click({ timeout: 2000 });
    else await pane.getByRole("button", { name: "More session actions" }).click({ timeout: 2000 });
    await page.getByRole("menuitem", { name: "Enable Skip permissions for this session" }).click({ timeout: 2000 });
  }).toPass({ timeout: 20_000 });
  return page.getByRole("dialog", { name: "Enable Skip permissions?" });
}
