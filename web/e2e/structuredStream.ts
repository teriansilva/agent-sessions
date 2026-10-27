/** A mocked API session whose running turn STREAMS (#1727): the journal's events page carries the
 *  turn's partial text events, at their own cursors, exactly as the operator's events route does;
 *  the snapshot shows the same reply and its `stream` marker. A test advances it one worker
 *  flush at a time (`flush`), adds rows (`addTurn`, `addTool`), and can make the snapshot lag,
 *  the events read fail, or the journal answer a cursor it no longer holds (409). */
import { expect, type Page } from "@playwright/test";

import { mockRoster } from "./roster";

export const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
export const TURN = "11111111-2222-4333-8444-555555555555";
export const URL_PATH = `/s/codex-api/${ID}`;
const SESSION = new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${ID}(/[a-z]+)?(\\?.*)?$`);

type Json = Record<string, unknown>;
export type StreamEvent = { cursor: number; kind: string; data: Json };

export interface StreamServer {
  revision: number;
  turns: Json[];
  events: StreamEvent[];
  state: "running" | "idle";
  /** Snapshot reads served, and events reads with the `after` / `wait` they asked. */
  snapshots: number;
  reads: { after: number; wait: string | null }[];
  /** While set, snapshots answer with this frozen copy (a snapshot that lags the journal). */
  frozen: Json | null;
  /** Fail this many events reads with a 503 (a lost connection). */
  failReads: number;
  /** Answer this many events reads with the journal's reset answer: a cursor it cannot serve. */
  resetReads: number;
}

export function operatorTurn(over: Json = {}): Json {
  return {
    kind: "operator",
    turn_id: TURN,
    operation_id: TURN,
    state: "running",
    text: "Walk me through the change.",
    ts: 1_790_000_000,
    reply: "",
    reply_ts: 1_790_000_010,
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    attachments: [],
    context: {},
    stream: { item: "m1" },
    ...over,
  };
}

/** The snapshot the server would answer now — a deep COPY, so a frozen one can never change under
 *  a later `flush` (which edits the live turns). */
export function snapshotOf(s: StreamServer): Json {
  return structuredClone({
    session_key: `codex-api:${ID}`,
    revision: s.revision,
    event_cursor: s.revision,
    cwd: "/home/u/proj",
    state: s.state,
    active_turn: s.state === "running" ? TURN : null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: s.turns,
    omitted_turns: 0,
    history_before: null,
    pending_requests: [],
    native: { native_id: "n1", worker: "w1", background_active: false },
    read_only: null,
  });
}

export async function mockStreamingSession(
  page: Page,
  turns: Json[],
  theme: "dark" | "light" = "dark",
): Promise<StreamServer> {
  const s: StreamServer = {
    revision: 4,
    turns,
    events: [],
    state: turns.some((t) => t.state === "running") ? "running" : "idle",
    snapshots: 0,
    reads: [],
    frozen: null,
    failReads: 0,
    resetReads: 0,
  };
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        theme,
        agent_defaults: { bypass: false },
      },
    }),
  );
  await page.route("**/api/sessions**", (r) =>
    r.fulfill({ json: { sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } } }),
  );
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  await page.route(SESSION, async (r) => {
    const url = new URL(r.request().url());
    const sub = url.pathname.match(/\/(events|history)$/)?.[1] ?? "";
    if (sub === "events") {
      const after = Number(url.searchParams.get("after") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? 100);
      s.reads.push({ after, wait: url.searchParams.get("wait") });
      if (s.failReads > 0) {
        s.failReads -= 1;
        return r.fulfill({ status: 503, json: { detail: "unavailable" } });
      }
      if (s.resetReads > 0 || after > s.revision) {
        s.resetReads = Math.max(0, s.resetReads - 1);
        return r.fulfill({ status: 409, json: { detail: "native event cursor is ahead of this journal" } });
      }
      const events = s.events.filter((e) => e.cursor > after).slice(0, limit);
      const next = events.length === limit ? events[events.length - 1].cursor : s.revision;
      return r.fulfill({
        json: { session_key: `codex-api:${ID}`, revision: s.revision, next_cursor: next, events },
      });
    }
    if (sub === "") {
      s.snapshots += 1;
      return r.fulfill({ json: s.frozen ?? snapshotOf(s) });
    }
    return r.fulfill({ status: 404, json: { detail: "not mocked" } });
  });
  return s;
}

function turnOf(s: StreamServer, id: string): Json {
  const t = s.turns.find((x) => x.turn_id === id);
  if (!t) throw new Error(`no turn ${id}`);
  return t;
}

/** One worker flush: a partial text event for `turn` (its own record), and the reply it builds. */
export function flush(s: StreamServer, text: string, turn = TURN, item = "m1"): void {
  s.revision += 1;
  s.events.push({
    cursor: s.revision,
    kind: "text",
    data: { operation_id: turn, native_turn_id: "n", item_id: item, text, partial: true, truncated: false },
  });
  const t = turnOf(s, turn);
  t.reply = `${t.reply ?? ""}${text}`;
}

/** A structural record: something only a snapshot shows (a new turn, a tool row, an end). */
function structural(s: StreamServer, kind: string, data: Json): void {
  s.revision += 1;
  s.events.push({ cursor: s.revision, kind, data });
}

export function addTurn(s: StreamServer, turn: Json): void {
  s.turns = [...s.turns, turn];
  // A queued claim is a private record: the cursor moves with no event (a gap).
  s.revision += 1;
}

export function addTool(s: StreamServer, id: string, turn = TURN): void {
  const t = turnOf(s, turn);
  t.tools = [...((t.tools as Json[]) ?? []), { id, name: "command", outcome: "", summary: `npm test -- ${id}` }];
  structural(s, "tool", { operation_id: turn, item_id: id, tool: "command" });
}

export function complete(s: StreamServer, turn = TURN): void {
  const t = turnOf(s, turn);
  t.state = "completed";
  delete t.stream;
  s.state = "idle";
  structural(s, "turn_completed", { operation_id: turn, state: "completed" });
}

export const log = (page: Page) => page.getByTestId("api-log");

/** Distance from the log's bottom. */
export const gap = (page: Page) =>
  log(page).evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);

export async function atBottom(page: Page): Promise<void> {
  await expect.poll(() => gap(page)).toBeLessThanOrEqual(3);
}

/** Record, from now on, whether any newly faded word (`.nj-word`) entered the log. */
export async function watchWords(page: Page): Promise<() => Promise<number>> {
  await log(page).evaluate((el) => {
    const w = window as unknown as { __words: number };
    w.__words = 0;
    new MutationObserver((records) => {
      for (const r of records)
        for (const n of r.addedNodes)
          if (n instanceof Element && (n.matches(".nj-word") || n.querySelector(".nj-word"))) w.__words += 1;
    }).observe(el, { childList: true, subtree: true });
  });
  return () => page.evaluate(() => (window as unknown as { __words: number }).__words);
}

export function paragraphs(count: number, from = 1): string {
  return Array.from({ length: count }, (_, i) => `Progress paragraph ${i + from}.`).join("\n\n");
}
