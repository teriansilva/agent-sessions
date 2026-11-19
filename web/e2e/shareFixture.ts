import { expect, type Locator, type Page, type Route } from "@playwright/test";
import { mockRoster } from "./roster";
import { animationsSettled } from "./nightjarShell";
import { TOUCH_FLOOR } from "./touchTarget";
import { setupBench } from "./terminal/harness";
import { SHARE_CHIP } from "./headActions";
import { remotePath } from "../src/lib/federation";

// The in-test model of `routes/federation.py`'s grant, suggestion and remote routes, and the
// helpers around it, shared by the specs that drive the sharing UI: `share-peer.spec.ts` (an API
// session, view, #2055), `peer-input.spec.ts` (suggest and send, #2061) and
// `share-terminal.spec.ts` (a terminal's screen, #2060). One model, so they cannot drift.

export const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
export const ID2 = "6c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10";
export const KEY = `codex-api:${ID}`;
export const URL_PATH = `/s/codex-api/${ID}`;
export const TITLE = "Refactor the importer";
export const TITLE2 = "Nightly report draft";
export const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
export const RENDER = "c4vy2sxqe7kdm3nzh6pbw5taj2ruo7gf";
export const LAPTOP = "b7nd4qzse2vkx6mhp3ytw5caj4ruo2gf";
export const OLD = "t2hy6wfnc5qdk7ezr3mbx4sav6pju2lo";
export const GRANT_A = "g7Jx9aBcDeFgHiJkLmNoPq";
export const GRANT_B = "h8Ky0bCdEfGhIjKlMnOpQr";
export const GRANT_OFF = "j9Lz1cDeFgHiJkLmNoPqRs";
export const CSRF = "operator-csrf";
export const DISCLOSURE =
  "The other instance sees this conversation as you see it here, without your secrets we know of. A secret we don't know about is not hidden.";
export const DISCLOSURE_DETAIL =
  "It gets the turns and the tool activity. Never the folder, the model settings or files; an image shows as its name only.";

export const NOW = () => Date.now() / 1000;
export const DAY = 86400;
export const dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
export const date = (epochS: number) => dayMonth.format(new Date(epochS * 1000));

export type Json = Record<string, unknown>;

export interface Grant extends Json {
  grant_id: string;
  session_key: string;
  peer_id: string;
  peer_name: string;
  level: string;
  expires_at: number;
  revision: number;
  state: string;
  title?: string;
}

export interface Answer {
  status?: number;
  json?: unknown;
  /** Hold the answer until this settles (a slow owner). */
  hold?: Promise<void>;
  /** Named, so a test can tell when a held answer was finally sent. */
  label?: string;
}

export const peer = (peer_id: string, name: string, over: Json = {}): Json => ({
  peer_id,
  name,
  state: "trusted",
  connected: true,
  refused: false,
  reachable_by: ["direct"],
  created_at: NOW() - 9 * DAY,
  last_seen_at: NOW() - 10,
  revision: 1,
  ...over,
});

export const turn = (id: string, text: string, reply: string, over: Json = {}): Json => ({
  kind: "operator",
  turn_id: id,
  state: "completed",
  delivery: null,
  text,
  text_truncated: false,
  reply,
  reply_ts: 1_800_000_000,
  reply_truncated: false,
  attachments: [],
  tools: [],
  tools_truncated: false,
  author: null,
  ...over,
});

/** A turn of the OWNER's own pane (`StructuredTurn`). */
export const localTurn = (id: string, text: string, reply: string | null, over: Json = {}): Json => ({
  kind: "operator",
  turn_id: id,
  operation_id: id,
  state: "completed",
  text,
  reply,
  ts: Math.floor(NOW()) - 300,
  reply_ts: reply === null ? null : Math.floor(NOW()) - 280,
  text_truncated: false,
  reply_truncated: false,
  reason: null,
  attachments: [],
  tools: [],
  tools_truncated: false,
  author: null,
  ...over,
});

export const snapshot = (turns: Json[], over: Json = {}): Json => ({
  link: "L".repeat(24),
  revision: 5,
  event_cursor: 5,
  state: "idle",
  active_turn: null,
  model: null,
  turns,
  omitted_turns: 0,
  pending_requests: [],
  read_only: false,
  images: false,
  can_send: false,
  mission_held: false,
  background: false,
  ...over,
});

export const QUIET: Answer = { json: { link: "L".repeat(24), revision: 5, next_cursor: 5, events: [] } };
export const MOVED: Answer = {
  json: { link: "L".repeat(24), revision: 6, next_cursor: 6, events: [{ cursor: 6, kind: "text" }] },
};
export const OFFLINE: Answer = {
  status: 503,
  json: { detail: "the instance that shares this session is not connected", code: "peer_offline" },
};
export const GONE: Answer = {
  status: 404,
  json: { detail: "this session is not shared with this instance", code: "no_grant" },
};

// ---- a shared terminal's screen (#2060) ----
export const TERM_ID = "7c1e3d2f-9a4b-4d8e-8f32-7b5c4d3e2f10";
export const TERM_KEY = `claude:${TERM_ID}`;
export const TERM_URL = `/s/claude/${TERM_ID}`;
export const TERM_TITLE = "Deploy the staging stack";
export const GRANT_T = "k0Ma2dEfGhIjKlMnOpQrSt";
export const GRANT_T2 = "m1Nb3eFgHiJkLmNoPqRsTu";
export const FP_A = "a".repeat(32);
export const FP_B = "b".repeat(32);
export const MASK = "█";
export const SCREEN_LINES = [
  "> Roll the staging stack to the new image and check the health route.",
  "",
  "* Reading compose/staging.yml",
  "* Running: docker compose -f compose/staging.yml pull",
  "    web      Pulled",
  "    worker   Pulled",
  "* Checking the health route ... 200 OK (41 ms)",
  "* Reading .env.staging",
  "    APP_ENV=staging",
  `    API_TOKEN=${MASK.repeat(8)}`,
  "    WORKERS=4",
];

/** A frame as `terminal.frame` answers it. */
export const frame = (over: Json = {}): Json => ({
  fingerprint: FP_A,
  cols: 100,
  rows: 30,
  lines: SCREEN_LINES,
  settled: true,
  ...over,
});
export const SETTLING: Answer = { json: { settling: true } };
export const unchanged = (fingerprint: string): Answer => ({ json: { fingerprint, unchanged: true } });
export const SCREEN_UNAVAILABLE: Answer = {
  status: 503,
  json: { detail: "the other instance could not answer right now", code: "unavailable" },
};

export const RENDER_TURNS = [
  turn(
    "aaaaaaaa-0000-4000-8000-000000000001",
    "The render queue stalls when two large jobs land together. Find out why.",
    "The scheduler takes one lock for the whole batch, so a second large job waits.",
    {
      attachments: [{ name: "diagram.png", mime: "image/png" }],
      tools: [
        { name: "read", outcome: "ok", summary: "queue/scheduler.py" },
        { name: "run", outcome: "ok", summary: "tests/queue · 12 passed" },
      ],
    },
  ),
  turn(
    "aaaaaaaa-0000-4000-8000-000000000002",
    "Go with the per-job lock. Keep the batch size as it is.",
    "Done. The lock is per job now and the queue tests pass.",
  ),
];

/** The server, as far as these pages see it. */
export class FakeShare {
  owner = true;
  peers: Json[] = [
    peer(GPU, "gpu-box"),
    peer(RENDER, "render-box"),
    peer(LAPTOP, "laptop", { connected: false, last_seen_at: NOW() - 2 * 3600 }),
    peer(OLD, "old-desktop", { state: "revoked" }),
  ];
  grants: Grant[] = [];
  remote: Json[] = [
    this.row(GPU, "gpu-box", GRANT_A, "Tune the render queue", true),
    this.row(GPU, "gpu-box", GRANT_B, "Color pipeline notes", true, 30),
    this.row(LAPTOP, "laptop", GRANT_OFF, "Field recording index", false),
  ];
  writes: { method: string; path: string; body: Json | null; csrf: string | undefined }[] = [];
  refuse: Record<string, { status: number; detail: string }> = {};
  reads: { kind: string; peer: string; grant: string; search: string }[] = [];
  sent: string[] = [];
  listReads = 0;
  sessionQueries: string[] = [];
  // ---- suggest and send (#2061) ----
  /** A mission holds the owner's session: the row says so, and the server refuses Send. */
  mission = false;
  /** Coded refusals of the next matching write: `"POST /grants/<id>/revoke"` → status + body. */
  refuseCoded: Record<string, { status: number; json: Json; times?: number }> = {};
  /** What peers suggested for the owner's session (`GET /suggestions`), any order. */
  suggestions: Json[] = [];
  /** How a decision is answered; `undefined` = the default (approve → approved, reject → rejected). */
  onDecide: (id: string, verb: "approve" | "reject", body: Json | null) => Answer | undefined = () => undefined;
  /** The owner's local conversation (the structured snapshot's turns). */
  localTurns: Json[] = [];
  /** The bell's rows. */
  notifications: Json[] = [];
  /** This instance's suggestions per remote grant, as the owner lists them (no text). */
  own: Record<string, Json[]> = {};
  /** How a write to a remote session is answered; `undefined` = the default below. */
  onWrite: (kind: "suggest" | "turns" | "withdraw", grant: string, body: Json | null, n: number) => Answer | undefined =
    () => undefined;
  /** How a status read (`…/operations/<key>`) is answered. Default: unknown. */
  onStatus: (key: string, grant: string, n: number) => Answer = () => ({ json: { known: false } });
  statusReads: string[] = [];
  private made = 0;
  private async answer(route: Route, answer: Answer, fallbackStatus = 200) {
    if (answer.hold) await answer.hold;
    try {
      await route.fulfill({ status: answer.status ?? fallbackStatus, json: answer.json ?? {} });
    } catch {
      // The page gave the request up while it was held: nothing to answer.
    }
    if (answer.label) this.sent.push(answer.label);
  }
  suggestion(over: Json = {}): Json {
    this.made += 1;
    return {
      suggestion_id: `sugg${this.made}`.padEnd(22, "x"),
      grant_id: "grant1".padEnd(22, "x"),
      session_key: KEY,
      peer_id: GPU,
      peer_name: "gpu-box",
      actor: "",
      text: "Please also keep the old batch entry point for one release.",
      state: "pending",
      approved_text: null,
      approved_by: null,
      approved_at: null,
      edited: false,
      handoff: null,
      end_reason: null,
      created_at: NOW() - 60 + this.made,
      updated_at: NOW() - 60 + this.made,
      expires_at: NOW() + DAY,
      ...over,
    };
  }
  ownRow(grant: string, over: Json = {}): Json {
    this.made += 1;
    const row = {
      suggestion_id: `mine${this.made}`.padEnd(22, "x"),
      client_key: "",
      state: "pending",
      handoff: null,
      created_at: NOW() - 30 + this.made,
      updated_at: NOW() - 30 + this.made,
      ...over,
    };
    this.own[grant] = [row, ...(this.own[grant] ?? [])];
    return row;
  }
  /** How a remote read is answered. The default is a live session with two turns. */
  onRead: (kind: string, grant: string, url: URL, n: number) => Answer = (kind) =>
    kind === "snapshot"
      ? { json: snapshot(RENDER_TURNS) }
      : kind === "events"
        ? QUIET
        : kind === "frame"
          ? { json: frame() }
          : GONE;
  private counts: Record<string, number> = {};

  row(peerId: string, name: string, grant: string, title: string, connected: boolean, days = 7): Json {
    return {
      key: `peer:${peerId}/codex-api:${grant}`,
      peer_id: peerId,
      peer_name: name,
      connected,
      grant_id: grant,
      engine: "codex-api",
      native_id: "9d8c7b6a-0000-4000-8000-000000000000",
      title,
      level: "view",
      expires_at: NOW() + days * DAY,
      revision: 1,
      seen_at: NOW() - 30,
    };
  }
  /** A terminal a peer shares (#2060): the same row with the server's `kind`. */
  screenRow(peerId: string, name: string, grant: string, title: string, connected: boolean, days = 7): Json {
    return {
      ...this.row(peerId, name, grant, title, connected, days),
      key: `peer:${peerId}/claude:${grant}`,
      engine: "claude",
      kind: "terminal",
    };
  }
  grant(peerId: string, over: Partial<Grant> = {}): Grant {
    const name = String(this.peers.find((p) => p.peer_id === peerId)?.name ?? "");
    return {
      grant_id: `grant${this.grants.length + 1}`.padEnd(22, "x"),
      session_key: KEY,
      peer_id: peerId,
      peer_name: name,
      level: "view",
      expires_at: NOW() + 7 * DAY,
      revision: 1,
      state: "live",
      created_at: NOW(),
      ended_at: null,
      end_reason: null,
      ...over,
    };
  }
  list(grants: Grant[]): Json {
    return {
      grants,
      generation: 1,
      levels: ["view", "suggest", "send"],
      default_level: "view",
      default_expires_in: { view: 7 * DAY, suggest: 7 * DAY, send: 3600 },
      max_expires_in: 90 * DAY,
      max_expires_in_by_level: { view: 90 * DAY, suggest: 30 * DAY, send: DAY },
      send_risk: "The other instance can instruct the agent with this session's permissions.",
    };
  }

  async handle(route: Route) {
    const req = route.request();
    const method = req.method();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^.*\/api\/federation/, "") || "/";
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    let m: RegExpMatchArray | null;
    if (method === "GET") {
      if (path === "/") {
        return json({
          owner: this.owner,
          self: { peer_id: "k3wq7n2bxd5mr4tzh6pac2vyj7uef3so", name: "studio", reachable_by: ["direct"] },
          peers: this.peers,
          invites: [],
          pairings: [],
        });
      }
      if (path === "/grants") {
        const session = url.searchParams.get("session");
        const peerId = url.searchParams.get("peer");
        const live = this.grants.filter(
          (g) => (!session || g.session_key === session) && (!peerId || g.peer_id === peerId),
        );
        // Only the per-peer list carries titles, as on the server.
        const untitled = (g: Grant): Grant => {
          const copy = { ...g };
          delete copy.title;
          return copy;
        };
        return json(this.list(peerId ? live : live.map(untitled)));
      }
      if (path === "/remote") {
        this.listReads++;
        return json({ remote: this.remote });
      }
      if (path === "/suggestions") {
        const session = url.searchParams.get("session");
        return json({ suggestions: this.suggestions.filter((x) => !session || x.session_key === session) });
      }
      if ((m = path.match(/^\/remote\/([^/]+)\/([^/]+)\/suggestions$/))) {
        this.reads.push({ kind: "suggestions", peer: m[1], grant: m[2], search: url.search });
        const custom = this.onRead("suggestions", m[2], url, 0);
        // The default `onRead` knows three kinds; anything else it answers is a test's own.
        if (custom !== GONE) return this.answer(route, custom);
        return json({ suggestions: this.own[m[2]] ?? [] });
      }
      if ((m = path.match(/^\/remote\/([^/]+)\/([^/]+)\/operations\/([^/]+)$/))) {
        const key = decodeURIComponent(m[3]);
        this.statusReads.push(key);
        const n = (this.counts[`status/${key}`] = (this.counts[`status/${key}`] ?? 0) + 1);
        return this.answer(route, this.onStatus(key, m[2], n));
      }
      if ((m = path.match(/^\/remote\/([^/]+)\/([^/]+)\/(snapshot|events|history|frame)$/))) {
        const [, peerId, grant, kind] = m;
        this.reads.push({ kind, peer: peerId, grant, search: url.search });
        const n = (this.counts[`${grant}/${kind}`] = (this.counts[`${grant}/${kind}`] ?? 0) + 1);
        const answer = this.onRead(kind, grant, url, n);
        if (answer.hold) await answer.hold;
        try {
          await json(answer.json ?? {}, answer.status ?? 200);
        } catch {
          // The page gave the request up while it was held: nothing to answer.
        }
        if (answer.label) this.sent.push(answer.label);
        return;
      }
      return json({ detail: "not mocked" }, 404);
    }
    const body = (req.postDataJSON() ?? null) as Json | null;
    this.writes.push({ method, path, body, csrf: req.headers()["x-csrf-token"] });
    if (req.headers()["x-csrf-token"] !== CSRF) return json({ detail: "bad csrf" }, 403);
    const refusal = this.refuse[`${method} ${path}`];
    if (refusal) {
      delete this.refuse[`${method} ${path}`];
      return json({ detail: refusal.detail, code: "conflict" }, refusal.status);
    }
    const coded = this.refuseCoded[`${method} ${path}`];
    if (coded) {
      if ((coded.times ?? 1) <= 1) delete this.refuseCoded[`${method} ${path}`];
      else coded.times = (coded.times ?? 1) - 1;
      return json(coded.json, coded.status);
    }
    // As `grants.py`: Send exists only on `accept_risk: true` in THIS request, and never for a
    // session a mission holds.
    const wants = (m_: Json | null, current = "view") => String(m_?.level ?? current);
    const refusedSend = (level: string): [Json, number] | null => {
      if (level !== "send") return null;
      if (body?.accept_risk !== true) return [{ detail: "Send accept_risk: true to grant it.", code: "risk" }, 422];
      if (this.mission) return [{ detail: "this session is part of a mission", code: "held" }, 409];
      return null;
    };
    if (method === "POST" && (m = path.match(/^\/suggestions\/([^/]+)\/(approve|reject)$/))) {
      const [, id, verb] = m as unknown as [string, string, "approve" | "reject"];
      const custom = this.onDecide(id, verb, body);
      if (custom) return this.answer(route, custom);
      const row = this.suggestions.find((x) => x.suggestion_id === id);
      if (!row) return json({ detail: "unknown suggestion", code: "not_found" }, 404);
      if (row.state !== "pending")
        return json({ detail: `this suggestion is already ${row.state}`, code: "conflict", state: row.state }, 409);
      if (verb === "approve") {
        const text = typeof body?.text === "string" ? body.text : row.text;
        Object.assign(row, { state: "approved", approved_text: text, edited: text !== row.text, handoff: "sent" });
        this.localTurns = [
          ...this.localTurns,
          localTurn(`bbbbbbbb-0000-4000-8000-${String(this.localTurns.length).padStart(12, "0")}`, String(text), null, {
            author: { name: row.peer_name, via: "peer", ...(row.edited ? { edited: true } : {}) },
          }),
        ];
      } else Object.assign(row, { state: "rejected", end_reason: "rejected" });
      return json({ suggestion: row });
    }
    if (method === "POST" && (m = path.match(/^\/remote\/([^/]+)\/([^/]+)\/(suggest|turns)$/))) {
      const [, , grant, kind] = m as unknown as [string, string, string, "suggest" | "turns"];
      const n = (this.counts[`${grant}/${kind}`] = (this.counts[`${grant}/${kind}`] ?? 0) + 1);
      const custom = this.onWrite(kind, grant, body, n);
      if (custom) return this.answer(route, custom, kind === "suggest" ? 201 : 202);
      if (kind === "turns") return json({ operation_id_known: true, handoff: "sent" }, 202);
      const before = (this.own[grant] ?? []).find((x) => x.client_key === body?.client_key);
      const row = before ?? this.ownRow(grant, { client_key: String(body?.client_key) });
      return json({ suggestion_id: row.suggestion_id, state: row.state }, 201);
    }
    if (method === "POST" && (m = path.match(/^\/remote\/([^/]+)\/([^/]+)\/suggestions\/([^/]+)\/withdraw$/))) {
      const [, , grant, id] = m;
      const custom = this.onWrite("withdraw", grant, body, 0);
      if (custom) return this.answer(route, custom);
      const row = (this.own[grant] ?? []).find((x) => x.suggestion_id === id);
      if (!row) return json({ detail: "no such suggestion", code: "not_found" }, 404);
      if (row.state !== "pending") return json({ detail: "decided first", code: "conflict" }, 409);
      row.state = "withdrawn";
      return json({ suggestion_id: id, state: "withdrawn" });
    }
    if (method === "POST" && path === "/grants") {
      // As on the server: the session's key says what is shared, and the grant stores its kind.
      // A terminal is shared View only (`grants.create`), whatever the request says.
      const key = String(body?.session_key ?? KEY);
      const terminal = !key.startsWith("codex-api:");
      if (terminal && body?.level !== "view")
        return json({ detail: "a terminal session can only be viewed", code: "invalid" }, 422);
      const no = refusedSend(wants(body));
      if (no) return json(...no);
      const made = this.grant(String(body?.peer_id), {
        session_key: key,
        kind: terminal ? "terminal" : "api",
        level: wants(body),
        expires_at: NOW() + Number(body?.expires_in),
        title: terminal ? TERM_TITLE : TITLE,
      });
      this.grants.push(made);
      return json({ grant: made, generation: 2 }, 201);
    }
    if (method === "PATCH" && (m = path.match(/^\/grants\/([^/]+)$/))) {
      const g = this.grants.find((x) => x.grant_id === m![1]);
      if (!g) return json({ detail: "unknown grant" }, 404);
      if (g.revision !== body?.revision) return json({ detail: "the grant changed" }, 409);
      if (g.kind === "terminal" && wants(body, g.level) !== "view")
        return json({ detail: "a terminal session can only be viewed", code: "invalid" }, 422);
      const no = refusedSend(wants(body, g.level));
      if (no) return json(...no);
      g.level = wants(body, g.level);
      g.expires_at = NOW() + Number(body?.expires_in);
      g.revision += 1;
      return json({ grant: g, generation: 3 });
    }
    if (method === "POST" && (m = path.match(/^\/grants\/([^/]+)\/revoke$/))) {
      const g = this.grants.find((x) => x.grant_id === m![1]);
      if (!g) return json({ detail: "unknown grant" }, 404);
      this.grants = this.grants.filter((x) => x !== g);
      return json({ grant: { ...g, state: "revoked" }, generation: 4 });
    }
    return json({ detail: "not mocked" }, 404);
  }
}

export async function setup(
  page: Page,
  prepare?: (fake: FakeShare) => void,
  theme: "dark" | "light" = "dark",
): Promise<FakeShare> {
  const fake = new FakeShare();
  prepare?.(fake);
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await page.route("**/api/**", (r) => r.fulfill({ status: 404, json: { detail: "not mocked" } }));
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: CSRF,
        new_session_engines: ["codex-api"],
        terminal_backend: "ws",
        auth_mode: "single-user",
        default_project: "/home/u/proj",
        overview_expanded: [],
        projects_hidden: [],
        theme,
      },
    }),
  );
  await page.route("**/api/version", (r) => r.fulfill({ json: { version: "test" } }));
  const local = (id: string, title: string, ago: number) => ({
    id: `codex-api:${id}`,
    engine: "codex-api",
    uuid: id,
    short_uuid: id.slice(0, 8),
    cwd: "/home/u/proj",
    project: { kind: "project", id: "p1", name: "importer", color: "#ffb000" },
    last_mtime: Math.floor(NOW()) - ago,
    first_user_message: title,
    title,
    sticky: false,
    archived: false,
    ai_summary: "",
    mission: id === ID && fake.mission ? { id: "msn_1", title: "Ship the importer", state: "running" } : null,
  });
  const rows = [local(ID, TITLE, 120), local(ID2, TITLE2, 3600)];
  await page.route("**/api/sessions**", (r) => {
    const url = new URL(r.request().url());
    fake.sessionQueries.push(url.search);
    // The server's own filtering, as far as the list here needs it: the title search and the
    // archived tab. A remote row is no part of this answer under any filter.
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const archived = url.searchParams.get("archived") === "1" || url.searchParams.get("archived") === "true";
    const sessions = archived ? [] : rows.filter((s) => !q || s.title.toLowerCase().includes(q));
    return r.fulfill({
      json: { sessions, next_offset: null, total: sessions.length, facets: { projects: [], engines: [] } },
    });
  });
  for (const row of rows) {
    await page.route(new RegExp(`/api/sessions/codex-api(?::|%3A)${row.uuid}$`), (r) => r.fulfill({ json: row }));
    await page.route(
      new RegExp(`/api/structured/sessions/codex-api(?::|%3A)${row.uuid}(/[a-z]+)?(\\?.*)?$`),
      (r) => {
        const sub = r.request().url().match(/\/(events)(\?|$)/)?.[1];
        // The journal's revision moves with the conversation, so a turn a decision caused is
        // read by the pane's own cursor loop.
        const revision = 4 + (row.uuid === ID ? fake.localTurns.length : 0);
        if (sub === "events")
          return r.fulfill({ json: { session_key: row.id, revision, next_cursor: revision, events: [] } });
        return r.fulfill({
          json: {
            session_key: row.id,
            revision,
            event_cursor: revision,
            cwd: "/home/u/proj",
            state: "idle",
            active_turn: null,
            model_requested: null,
            model_effective: "gpt-5-codex",
            turns: row.uuid === ID ? fake.localTurns : [],
            omitted_turns: 0,
            pending_requests: [],
            native: { native_id: "n1", worker: null, background_active: false },
            read_only: null,
          },
        });
      },
    );
  }
  await page.route(/\/api\/projects(\?.*)?$/, (r) => r.fulfill({ json: { projects: [] } }));
  await page.route(/\/api\/pulse\/notifications(\?.*)?$/, (r) =>
    r.fulfill({
      json: {
        notifications: fake.notifications,
        unread: fake.notifications.filter((n) => !n.read).length,
        settled: [],
        uncertain: 0,
      },
    }),
  );
  await mockRoster(page, {
    overrides: {
      "codex-api": {
        present: true,
        supports_new: true,
        api: { kind: "codex-app-server", source: "codex", unavailable_reason: null, can_bypass: true },
      },
    },
  });
  await page.route(/\/api\/federation(\/.*)?(\?.*)?$/, (r) => fake.handle(r));
  return fake;
}

export const isMobile = (page: Page) => (page.viewportSize()?.width ?? 1280) <= 800;

/** The session list: a column on a wide screen, the drawer on a phone (opened if it is shut). */
export async function showList(page: Page) {
  const list = page.locator("aside.sidebar");
  if (isMobile(page)) {
    const toggle = page.locator("header .navToggle");
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await animationsSettled(page);
  }
  await expect(list).toBeVisible();
  return list;
}

/** Select one of the session list's scopes (#2198): Active, Archived, or Shared (the sessions
 *  peers share; offered while one is shared). The scope is kept for the page's lifetime, so a test
 *  that leaves Shared for a local row selects Active first. */
export async function selectScope(list: Locator, scope: "active" | "archived" | "shared") {
  const tab = list.getByTestId(`scope-tab-${scope}`);
  await expect(tab).toBeVisible();
  if ((await tab.getAttribute("aria-selected")) !== "true") await tab.click();
  await expect(tab).toHaveAttribute("aria-selected", "true");
}

/** The session list on its Shared tab: where the remote rows are since #2198. THE one way the
 *  sharing specs reach a remote row in the list. */
export async function showShared(page: Page) {
  const list = await showList(page);
  await selectScope(list, "shared");
  return list;
}

/** The session list on Active: this instance's own rows. */
export async function showLocal(page: Page) {
  const list = await showList(page);
  await selectScope(list, "active");
  return list;
}

/** Run a head action wherever the header put it: inline, the "…" fold, or the ≤800px menu.
 *  Returns the control that opened it (what focus must come back to). */
export async function headAction(page: Page, aria: string): Promise<Locator> {
  const pane = page.getByTestId("structured-pane");
  const trigger = pane.getByTestId("head-actions-menu");
  const chip = pane.locator("[data-head-action]").first();
  await expect(trigger.or(chip).first()).toBeVisible();
  let opener: Locator = trigger;
  let attempt = 0;
  await expect(async () => {
    if (attempt++ > 0) await page.keyboard.press("Escape");
    if (await trigger.isVisible()) {
      opener = trigger;
      await trigger.click({ timeout: 2000 });
    } else {
      const inline = pane.locator("[data-head-action]").and(pane.getByRole("button", { name: aria }));
      if (await inline.isVisible()) {
        opener = inline;
        return inline.click({ timeout: 2000 });
      }
      // Public… and Peer… are the Share chip's two choices (#2173): on the bar the chip opens
      // them; folded, they are items of the "…" under a Share label.
      const share = pane.locator(SHARE_CHIP);
      opener =
        aria.startsWith("Share this ") && (await share.isVisible())
          ? share
          : pane.getByRole("button", { name: "More session actions" });
      await opener.click({ timeout: 2000 });
    }
    await page.getByRole("menuitem", { name: aria }).click({ timeout: 2000 });
  }).toPass({ timeout: 15_000 });
  return opener;
}

/** The owner's side for a terminal: the terminal bench (a fake `ws` terminal and its REST API)
 *  with the federation routes answered by the same model. */
export async function setupTerminal(
  page: Page,
  prepare?: (fake: FakeShare) => void,
  theme: "dark" | "light" = "dark",
): Promise<FakeShare> {
  const fake = new FakeShare();
  prepare?.(fake);
  await page.addInitScript((t) => localStorage.setItem("tr-theme", t), theme);
  await setupBench(page, { sessions: [{ engine: "claude", uuid: TERM_ID, title: TERM_TITLE }] });
  await page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: CSRF,
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
        theme,
      },
    }),
  );
  await page.route(/\/api\/federation(\/.*)?(\?.*)?$/, (r) => fake.handle(r));
  return fake;
}

export const SHARE_PEER = "Share this session with a paired instance";

export async function openDialog(page: Page): Promise<{ dialog: Locator; opener: Locator }> {
  await page.goto(URL_PATH);
  await expect(page.getByTestId("structured-empty")).toBeVisible();
  const opener = await headAction(page, SHARE_PEER);
  const dialog = page.getByRole("dialog", { name: "Share with a peer" });
  await expect(dialog).toBeVisible();
  await animationsSettled(page);
  return { dialog, opener };
}

export async function noHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** Every visible control in `scope` meets the 44 px floor (docs/design.md §8). */
export async function controlsAtLeast44(scope: Locator) {
  const controls = scope.locator("button:visible, a[href]:visible, input:visible, summary:visible");
  const n = await controls.count();
  expect(n).toBeGreaterThan(0);
  for (let i = 0; i < n; i++) {
    const control = controls.nth(i);
    const box = (await control.boundingBox())!;
    const what = (await control.getAttribute("aria-label")) || (await control.innerText()).trim();
    expect(box.height, `${what} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.width, `${what} width`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

export async function openPane(page: Page, grant = GRANT_A, peerId = GPU) {
  await page.goto(remotePath(peerId, grant));
  const pane = page.getByTestId("remote-pane");
  await expect(pane).toBeVisible();
  return pane;
}

/** A gate a test opens to let a held answer through. */
export function gate(): { promise: Promise<void>; open: () => void } {
  let open!: () => void;
  const promise = new Promise<void>((r) => (open = r));
  return { promise, open };
}

/** Two frames: whatever a just-delivered answer would have painted is painted by now. */
export const painted = (page: Page) =>
  page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
