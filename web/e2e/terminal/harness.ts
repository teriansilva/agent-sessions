// Isolated terminal bench (#301, slice 1). A self-contained environment to drive the console
// through every scenario WITHOUT a backend — so reliability is proven, not guessed at.
//
// Isolation by construction: the SPA is the local `vite preview`; ALL `/api` is mocked via
// page.route; the WebSocket is REPLACED in-page (addInitScript) before app code runs. There is
// no real dtach, no runtime dir, no real session — it cannot reach prod/staging. `attachTripwires`
// fails the test if any un-mocked request or a real WebSocket ever escapes.
import { type Page, expect } from "@playwright/test";

export interface BenchSession {
  engine: string;
  uuid: string;
  title: string;
}

export interface BenchOptions {
  sessions: BenchSession[];
  /** Lines of scroll-up "history" per session key (`engine:uuid`). Defaults to a short, in-viewport
   *  block with recognizable markers so a test can assert the right session's history is shown. */
  history?: Record<string, string[]>;
  /** Model the real agent: a resize that CHANGES the grid triggers a clear+repaint that wipes the
   *  injected scroll-up (the #299 mechanism). Default true — so the bench proves the #300 fix
   *  (connect-at-stable-size → no spurious resize → history survives). */
  wipeOnResizeChange?: boolean;
  /** Scroll-up lazy-load (#348 Phase 3): sequential responses for the mocked
   *  `GET /api/sessions/<sid>/history` route — the Nth fetch gets `lazyPages[N]`; past the
   *  end (or when omitted) the route answers the empty end-of-history shape. `delayMs`
   *  holds the response so a spec can observe the loading pill. `status` (e.g. 500)
   *  makes that call fail so a spec can exercise the error + retry pill. */
  lazyPages?: LazyHistoryPage[];
  /** Per-tab ownership role the fake server reports on connect (#184/#485). Default "owner";
   *  set "secondary" to model a read-only viewer (the take-over banner shows, the owner-only
   *  REPAINT control is hidden). */
  role?: "owner" | "secondary";
}

export interface LazyHistoryPage {
  ansi: string;
  cursor: number | null;
  has_more: boolean;
  delayMs?: number;
  status?: number;
}

const now = 1_700_000_000;

/** Default short history (fits the viewport, no scroll needed) with BEGIN/END markers + a live tail. */
export function defaultHistory(key: string): string[] {
  const id = key.replace(/[^a-z0-9]/gi, "");
  const lines = [`HIST ${id} BEGIN`];
  for (let i = 1; i <= 6; i++) lines.push(`HIST ${id} line ${i}`);
  lines.push(`HIST ${id} END`, `LIVE ${id} $ `);
  return lines;
}

/** Mock the REST API the SPA needs to boot + render a switchable session list. */
function mockApi(page: Page, sessions: BenchSession[]) {
  const items = sessions.map((s, i) => ({
    id: `${s.engine}:${s.uuid}`,
    engine: s.engine,
    uuid: s.uuid,
    short_uuid: s.uuid,
    cwd: "/home/u/proj",
    project: { kind: "folder", id: "/home/u/proj", name: "proj" },
    last_mtime: now - i,
    first_user_message: "",
    title: s.title,
    sticky: false,
    sort_key: i,
    archived: false,
  }));
  page.route("**/api/config", (r) =>
    r.fulfill({
      json: {
        csrf: "x",
        new_session_engines: ["claude"],
        terminal_backend: "ws",
        auth_mode: "none",
        overview_expanded: [],
        projects_hidden: [],
      },
    }),
  );
  page.route("**/api/version", (r) => r.fulfill({ json: { version: "bench" } }));
  page.route("**/api/engines", (r) => r.fulfill({ json: { engines: ["claude"] } }));
  page.route("**/api/system", (r) => r.fulfill({ json: {} }));
  page.route(/\/api\/folders(\?.*)?$/, (r) => r.fulfill({ json: { folders: [{ cwd: "/home/u/proj", label: "proj" }] } }));
  page.route("**/api/prefs", (r) => r.fulfill({ json: {} }));
  page.route("**/api/sessions**", (r) =>
    r.fulfill({
      json: { sessions: items, next_offset: null, total: items.length, facets: { projects: [{ kind: "folder", id: "/home/u/proj", name: "/home/u/proj" }], engines: ["claude"] } },
    }),
  );
}

/** The in-page fake terminal server. Speaks the real protocol: connect → history + seq; models the
 *  agent repaint-on-resize-change wipe (#299). Config is read from window.__BENCH__. */
function fakeWsScript() {
  return `
(() => {
  const cfg = window.__BENCH__ || { history: {}, wipeOnResizeChange: true };
  const enc = new TextEncoder();
  window.WebSocket = class {
    constructor(url) {
      window.__BENCH_LAST_WS__ = this; // test hook: drop the live socket to force a reconnect
      this.url = url; this.readyState = 0; this.binaryType = "blob";
      this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
      const u = new URL(url, location.href);
      const m = u.pathname.match(/\\/ws\\/term\\/([^?]+)/);
      this.key = m ? decodeURIComponent(m[1]) : "";
      this.have = Number(u.searchParams.get("have") || 0);
      this._cols = Number(u.searchParams.get("cols") || 80);
      this._rows = Number(u.searchParams.get("rows") || 24);
      setTimeout(() => {
        this.readyState = 1;
        this.onopen && this.onopen();
        // Active viewer (prod runs take-over on) → role frame, then the scroll-up history.
        this.onmessage && this.onmessage({ data: JSON.stringify({ t: "role", role: cfg.role || "owner" }) });
        const lines = (cfg.history && cfg.history[this.key]) || [];
        const s = lines.join("\\r\\n");
        if (this.have === 0 && s) {
          this.onmessage && this.onmessage({ data: enc.encode(s).buffer });
          this.onmessage && this.onmessage({ data: JSON.stringify({ t: "seq", n: enc.encode(s).length }) });
        }
      }, 5);
    }
    send(raw) {
      let msg; try { msg = JSON.parse(raw); } catch { return; }
      if (msg && msg.t === "r") {
        const changed = msg.cols !== this._cols || msg.rows !== this._rows;
        this._cols = msg.cols; this._rows = msg.rows;
        // The real agent repaints on a grid change → ESC[2J clears the screen, wiping the injected
        // scroll-up. The #300 fix avoids a spurious post-connect resize, so this must NOT fire.
        if (changed && cfg.wipeOnResizeChange) {
          const wipe = "\\x1b[2J\\x1b[H" + "LIVE (repainted) $ ";
          this.onmessage && this.onmessage({ data: enc.encode(wipe).buffer });
        }
      }
    }
    close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
  };
})();
`;
}

/** Fail the test if any real request escapes the mock — a hard isolation gate (#301). */
function attachTripwires(page: Page) {
  page.on("request", (req) => {
    const url = req.url();
    const ok =
      url.startsWith("http://localhost:") ||
      url.startsWith("http://127.0.0.1:") ||
      url.startsWith("data:") ||
      url.startsWith("blob:");
    if (!ok) throw new Error(`BENCH ISOLATION BREACH: un-mocked request escaped → ${url}`);
  });
  // window.WebSocket is replaced in-page, so Playwright should never see a real WS open.
  page.on("websocket", (ws) => {
    throw new Error(`BENCH ISOLATION BREACH: a real WebSocket opened → ${ws.url()}`);
  });
}

/** Boot the bench: tripwires + mocked /api + the in-page fake terminal server. Returns nothing;
 *  call before page.goto. */
/** Mock the paged transcript-history endpoint (#348 Phase 3). Registered AFTER the generic
 *  `/api/sessions**` route so it takes precedence (Playwright matches newest-first). Serves
 *  `pages` sequentially; exhausted/omitted → the empty end-of-history shape. */
function mockHistoryApi(page: Page, pages: LazyHistoryPage[] = []) {
  let call = 0;
  page.route(/\/api\/sessions\/[^/]+\/history(\?.*)?$/, async (route) => {
    const p = pages[call++] ?? { ansi: "", cursor: null, has_more: false };
    if (p.delayMs) await new Promise((r) => setTimeout(r, p.delayMs));
    if (p.status && p.status >= 400) {
      await route.fulfill({ status: p.status, json: { detail: "bench error" } });
      return;
    }
    await route.fulfill({ json: { ansi: p.ansi, cursor: p.cursor, has_more: p.has_more } });
  });
}

export async function setupBench(page: Page, opts: BenchOptions) {
  attachTripwires(page);
  mockApi(page, opts.sessions);
  mockHistoryApi(page, opts.lazyPages);
  const history = opts.history ?? Object.fromEntries(
    opts.sessions.map((s) => [`${s.engine}:${s.uuid}`, defaultHistory(`${s.engine}:${s.uuid}`)]),
  );
  await page.addInitScript(
    ({ history, wipeOnResizeChange, role }) => {
      (window as unknown as { __BENCH__: unknown }).__BENCH__ = { history, wipeOnResizeChange, role };
    },
    { history, wipeOnResizeChange: opts.wipeOnResizeChange ?? true, role: opts.role ?? "owner" },
  );
  await page.addInitScript(fakeWsScript());
}

/** Stream more live output from the fake server AFTER attach — i.e. simulate the agent
 *  printing while the user reads scrollback. Delivers raw bytes on the live bench WS (the
 *  same channel the attach replay used), so the client writes them as ordinary output.
 *  Used to prove streaming output never yanks a scrolled-up viewport. */
export async function pushOutput(page: Page, text: string) {
  await page.evaluate((t) => {
    const ws = (
      window as unknown as {
        __BENCH_LAST_WS__?: { onmessage?: (e: { data: ArrayBuffer }) => void };
      }
    ).__BENCH_LAST_WS__;
    if (!ws?.onmessage) throw new Error("BENCH: no live WS to push output from");
    ws.onmessage({ data: new TextEncoder().encode(t).buffer });
  }, text);
}

/** Assert the live terminal shows the given text (in the visible xterm rows). */
export async function expectTerminalShows(page: Page, text: string) {
  await expect(page.locator(".xterm-rows")).toContainText(text, { timeout: 5000 });
}

export async function expectTerminalHidden(page: Page, text: string) {
  await expect(page.locator(".xterm-rows")).not.toContainText(text, { timeout: 5000 });
}
