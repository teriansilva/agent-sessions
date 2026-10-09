import type { Page, Route } from "@playwright/test";
import { mockRoster } from "./roster";

// One conversation/UI contract for every native client (#1389).
export const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
export const ACTIVE = "11111111-2222-4333-8444-555555555555";
export const QUEUED = "22222222-2222-4333-8444-555555555555";
export const COMMAND = "python -m pytest tests/test_session.py -q";
export const clients = [
  ["codex-api", "steer"],
  ["claude-api", "interrupt"],
  ["opencode-api", "interrupt"],
] as const;
type Json = Record<string, unknown>;

export async function mockStructuredGuidance(page: Page, engine: string, mode: string, theme = "dark") {
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) => r.fulfill({ json: {
    csrf: "test", terminal_backend: "ws", auth_mode: "none", theme,
    overview_expanded: [], projects_hidden: [],
  } }));
  await page.route("**/api/sessions**", (r) => r.fulfill({ json: {
    sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] },
  } }));
  await mockRoster(page);
  const common = { reply: "", text_truncated: false, reply_truncated: false,
    tools_truncated: false, reason: null, tools: [] };
  const state = {
    session_key: `${engine}:${ID}`, revision: 4, get event_cursor(): number { return this.revision; }, cwd: "/home/u/demo",
    state: "running", active_turn: ACTIVE, model_requested: null,
    omitted_turns: 0, pending_requests: [] as Json[], read_only: null as string | null,
    native: { native_id: "native", worker: "worker", background_active: false, send_now: mode },
    turns: [
      { ...common, turn_id: ACTIVE, operation_id: ACTIVE, state: "running", text: "Check startup.",
        reply: "I’m checking parallel session startup.",
        tools: Array.from({ length: 12 }, (_, i) => ({ id: `tool-${i}`, name: "command",
          summary: i === 0 ? COMMAND : `rg -n 'session' src/file-${i}.py`, outcome: "completed" })) },
      { ...common, turn_id: QUEUED, operation_id: QUEUED, state: "queued", text: "Only inspect the files." },
    ] as Json[],
  };
  const calls: Json[] = [];
  let fail: "before" | "after" | null = null;
  let failureStatus: number | undefined;
  const failure = (r: Route) => failureStatus
    ? r.fulfill({ status: failureStatus, json: { detail: failureStatus === 409
      ? "no native worker is running this session" : "the request may still be applied" } })
    : r.abort("failed");
  const route = new RegExp(`/api/structured/sessions/${engine}(?::|%3A)${ID}(/[^?]+)?(\\?.*)?$`);
  await page.route(route, async (r) => {
    const sub = r.request().url().match(route)?.[1] ?? "";
    if (sub === "/events") return r.fulfill({ json: { revision: state.revision, next_cursor: state.revision, events: [] } });
    if (sub === "/send-now") {
      calls.push(r.request().postDataJSON());
      if (fail === "before") return failure(r);
      state.turns[1].delivery = mode === "steer" ? "steering" : "interrupting";
      if (mode === "steer") state.turns[1].state = "delivering";
      state.revision++;
      if (fail === "after") return failure(r);
      return r.fulfill({ json: { handoff: "sent", operation_id: calls.at(-1)!.operation_id } });
    }
    if (!sub) return r.fulfill({ json: state });
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return { state, calls, fail: (afterHandoff = false, status?: number) => {
    fail = afterHandoff ? "after" : "before";
    failureStatus = status;
  },
    recover: () => { fail = null; } };
}
