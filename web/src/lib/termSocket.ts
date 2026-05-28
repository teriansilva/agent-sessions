// Reconnect-resilient transport for the terminal websocket. This is the glitch-prone
// part the rebuild exists to fix, so it's a plain class with an injectable WebSocket
// factory — unit-tested with a fake socket, independent of xterm/DOM.
//
// Protocol (server = webterm.run): binary frames are raw PTY output; string frames are
// JSON control frames. The only control frame today is {"t":"seq","n":<total>}, the
// server's authoritative absolute byte offset. We track how many bytes we've consumed
// and reconnect with `?have=<offset>` so the server replays only the delta — the screen
// continues seamlessly across a drop instead of blanking or re-replaying everything.

export type TermStatus =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "reconnecting"; attempt: number }
  | { kind: "rejected"; reason: string };

export type TermRole = "owner" | "secondary";

export interface TermSocketHandlers {
  onOutput: (bytes: Uint8Array) => void;
  onStatus: (status: TermStatus) => void;
  /** Server reconciled this session to its real engine-qualified id (#127, opencode
   *  new-session). The client converges the URL/sidebar to `sid` (e.g.
   *  `opencode:ses_…`). Optional — only the new-session path emits it. */
  onId?: (sid: string) => void;
  /** Per-tab ownership protocol (#184 slice 3): the server's verdict on whether this
   *  WS holds the owner role or is a read-only secondary. Sent on connect, and again
   *  when a force takeover demotes the previous owner mid-session. */
  onRole?: (role: TermRole) => void;
}

// Deliberate server rejects — never reconnect on these (would hammer the backend).
// 4409 (BUSY: another writer holds the lock) is intentionally NOT here: we retry and
// end up attaching once the live master is up. Kept explicit, not a numeric range.
const NO_RETRY = new Set([4401, 4403, 4404, 4500]);
const REJECT_REASON: Record<number, string> = {
  4401: "session expired — please sign in again",
  4403: "blocked (origin mismatch)",
  4404: "session not found or ended",
  4500: "couldn’t start this session",
};

const BACKOFF_BASE_MS = 600;
const BACKOFF_MAX_MS = 10_000;

export type WsFactory = (url: string) => WebSocket;

export class TermSocket {
  private ws: WebSocket | null = null;
  /** Absolute count of PTY bytes consumed — sent back as `?have=` to resume. */
  private offset = 0;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  private readonly urlFor: (have: number) => string;
  private readonly handlers: TermSocketHandlers;
  private readonly wsFactory: WsFactory;

  constructor(
    urlFor: (have: number) => string,
    handlers: TermSocketHandlers,
    wsFactory: WsFactory = (u) => new WebSocket(u),
  ) {
    this.urlFor = urlFor;
    this.handlers = handlers;
    this.wsFactory = wsFactory;
  }

  /** Backoff for reconnect attempt N (0-based): 0.6s, 1.2s, 2.4s … capped at 10s. */
  backoffMs(attempt: number): number {
    return Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_MAX_MS);
  }

  /** Bytes consumed so far (the `have` offset). Exposed for tests/diagnostics. */
  get consumed(): number {
    return this.offset;
  }

  connect(): void {
    this.stopped = false;
    this.handlers.onStatus(
      this.attempt === 0 ? { kind: "connecting" } : { kind: "reconnecting", attempt: this.attempt },
    );
    const ws = this.wsFactory(this.urlFor(this.offset));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0; // a successful open resets the backoff
      this.handlers.onStatus({ kind: "connected" });
    };
    ws.onmessage = (ev: MessageEvent) => this.onMessage(ev.data);
    ws.onclose = (ev: CloseEvent) => this.onClose(ev.code);
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    };
  }

  private onMessage(data: unknown): void {
    if (typeof data === "string") {
      // Control frame. {"t":"seq","n"} sets our authoritative offset (the server's
      // total). Unknown control frames are ignored — never written to the terminal.
      try {
        const msg = JSON.parse(data) as {
          t?: string;
          n?: number;
          sid?: string;
          role?: TermRole;
        };
        if (msg.t === "seq" && typeof msg.n === "number") this.offset = msg.n;
        // {"t":"id","sid":"opencode:ses_…"} — the new-session reconcile result (#127).
        else if (msg.t === "id" && typeof msg.sid === "string") this.handlers.onId?.(msg.sid);
        // {"t":"role","role":"owner"|"secondary"} — per-tab claim verdict (#184).
        else if (msg.t === "role" && (msg.role === "owner" || msg.role === "secondary"))
          this.handlers.onRole?.(msg.role);
      } catch {
        /* ignore malformed control frame */
      }
      return;
    }
    const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : null;
    if (!bytes) return;
    this.offset += bytes.byteLength; // advance the resume offset by what we render
    this.handlers.onOutput(bytes);
  }

  private onClose(code: number): void {
    this.ws = null;
    if (this.stopped) return;
    if (NO_RETRY.has(code)) {
      this.handlers.onStatus({ kind: "rejected", reason: REJECT_REASON[code] ?? "unavailable" });
      return;
    }
    // Transient drop (incl. 4409 busy) → reconnect with capped backoff, resuming from
    // the consumed offset. Never clears the terminal; the server streams only the delta.
    const delay = this.backoffMs(this.attempt);
    this.attempt += 1;
    this.handlers.onStatus({ kind: "reconnecting", attempt: this.attempt });
    this.timer = setTimeout(() => this.connect(), delay);
  }

  /** Send a JSON message to the server ({t:'i',d} input, {t:'r',cols,rows} resize). */
  send(msg: Record<string, unknown>): boolean {
    // 1 === WebSocket.OPEN; use the literal so this stays usable where the global
    // WebSocket constructor isn't defined (e.g. jsdom test env).
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  /** Stop for good (component unmount): no further reconnects. */
  close(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* already closing */
      }
      this.ws = null;
    }
  }
}
