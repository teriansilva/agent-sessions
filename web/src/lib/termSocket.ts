// Reconnect-resilient transport for the terminal websocket. This is the glitch-prone
// part the rebuild exists to fix, so it's a plain class with an injectable WebSocket
// factory — unit-tested with a fake socket, independent of xterm/DOM.
//
// Protocol (server = webterm.run): binary frames are raw PTY output; string frames are
// JSON control frames. {"t":"seq","n":<total>} is the server's authoritative absolute
// byte offset: we track how many bytes we've consumed and reconnect with `?have=<offset>`
// so the server replays only the delta — the screen continues seamlessly across a drop
// instead of blanking or re-replaying everything. {"t":"hist","cursor":N} (#348) follows
// seq on a transcript attach and carries the exact turn boundary of the attach payload
// for the scroll-up lazy-loader.

export type TermStatus =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "reconnecting"; attempt: number }
  | { kind: "rejected"; reason: string };

export type TermRole = "owner" | "secondary";

/** The active viewer shown on the read-only take-over banner (#293/#434). `label` is the
 *  holder's display-only device name; `since` is a unix timestamp (seconds) of when they took it. */
export interface TermGateHolder {
  label: string;
  since?: number;
}

export interface TermSocketHandlers {
  onOutput: (bytes: Uint8Array) => void;
  onStatus: (status: TermStatus) => void;
  /** Server reconciled this session to its real engine-qualified id (#127, opencode
   *  new-session). The client converges the URL/sidebar to `sid` (e.g.
   *  `opencode:ses_…`). Optional — only the new-session path emits it. */
  onId?: (sid: string) => void;
  /** Per-tab ownership protocol (#184 slice 3 / #293 / #434): the server's verdict on
   *  whether this WS holds the owner role or is a read-only secondary. Sent on connect, and
   *  again when a force takeover demotes the previous owner mid-session. `holder` (set by the
   *  flag-on take-over path) names the active viewer for the read-only banner; absent on the
   *  in-memory #184 path. A `secondary` viewer streams read-only — never blank — and offers
   *  "Take over" (a reconnect with force=1). */
  onRole?: (role: TermRole, holder?: TermGateHolder | null) => void;
  /** Server sent the authoritative byte offset for the just-delivered attach/replay batch. */
  onSeq?: (n: number) => void;
  /** Scroll-up lazy-load first-page cursor (#348, Hermes #365 r2): sent right after `seq`
   *  when the attach payload came from the transcript renderer, carrying the EXACT turn
   *  index the payload starts at. The terminal seeds its HistoryLoader from it so the
   *  first /history request asks for `before=<cursor>` — the server never re-derives the
   *  attach boundary at a (possibly resized) later width. */
  onHist?: (cursor: number) => void;
}

// Deliberate server rejects — never reconnect on these (would hammer the backend).
// Close-code taxonomy (#346): 4401/4403 auth/origin, 4404 not-found, 4500 non-retryable
// launch misconfiguration — all terminal. Intentionally NOT here, so they reconnect with
// backoff: 4409 (BUSY: another writer holds the lock — retry until the live master is up)
// and 4502 (transient start failure: spawn timeout / EAGAIN under resource pressure — the
// condition clears, so the client must keep trying rather than die on a momentary blip).
// Kept explicit, not a numeric range.
const NO_RETRY = new Set([4401, 4403, 4404, 4500]);
const REJECT_REASON: Record<number, string> = {
  4401: "session expired — please sign in again",
  4403: "blocked (origin mismatch)",
  4404: "session not found or ended",
  4500: "couldn’t start this session",
};

const BACKOFF_BASE_MS = 600;
const BACKOFF_MAX_MS = 10_000;
// A hung WS handshake (network changed/died) can otherwise sit ~20–30s before the browser
// gives up and fires onclose. Bound it: if we're not open within this, close and retry (#236).
const CONNECT_TIMEOUT_MS = 8_000;

export type WsFactory = (url: string) => WebSocket;

// The factory used to open a terminal socket when a TermSocket isn't given an explicit one
// (the app's Terminal component uses the default). Same-origin `new WebSocket` by default;
// Home Free's connect page (#579 P4b) injects a mux-backed factory so the terminal `/ws`
// rides the blind relay. Behaviour-neutral when unset — the app can't tell it's tunneled.
const defaultWsFactory: WsFactory = (u) => new WebSocket(u);
let wsFactoryImpl: WsFactory = defaultWsFactory;
/** Route default terminal sockets through `fn` (Home Free tunnel), or back to `new WebSocket`. */
export function setWsFactory(fn: WsFactory | null): void {
  wsFactoryImpl = fn ?? defaultWsFactory;
}

export class TermSocket {
  private ws: WebSocket | null = null;
  /** Absolute count of PTY bytes consumed — sent back as `?have=` to resume. */
  private offset = 0;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  // Monotonic id of the CURRENT underlying socket — bumped on every successful open. A caller that
  // sends a multi-frame message (Compose: clear → paste → deferred Enter) can capture this and tell
  // whether a reconnect happened between frames, so it never fires a bare Enter onto a fresh socket
  // that never received the paste (the empty-compose bug #287).
  private connId = 0;
  // Set once a NO_RETRY reject (auth/origin/not-found/startup-failure) lands: terminal for this
  // socket's lifetime, so a later online/visible wake can't resurrect a deliberate reject (#236).
  // Reset only by a fresh TermSocket (the Terminal remounts per session / takeover).
  private rejected = false;

  private readonly urlFor: (have: number) => string;
  private readonly handlers: TermSocketHandlers;
  private readonly wsFactory: WsFactory;

  // Wake handlers (#236): when the device comes back online or the tab is refocused, retry
  // immediately instead of waiting out a pending backoff / hung handshake.
  private readonly onOnline = () => this.wake();
  private readonly onVisible = () => {
    if (
      typeof document === "undefined" ||
      document.visibilityState === "visible"
    )
      this.wake();
  };

  constructor(
    urlFor: (have: number) => string,
    handlers: TermSocketHandlers,
    wsFactory: WsFactory = (u) => wsFactoryImpl(u),
  ) {
    this.urlFor = urlFor;
    this.handlers = handlers;
    this.wsFactory = wsFactory;
    if (typeof window !== "undefined")
      window.addEventListener("online", this.onOnline);
    if (typeof document !== "undefined")
      document.addEventListener("visibilitychange", this.onVisible);
  }

  /** Backoff for reconnect attempt N (0-based): 0.6s, 1.2s, 2.4s … capped at 10s. */
  backoffMs(attempt: number): number {
    return Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_MAX_MS);
  }

  /** Bytes consumed so far (the `have` offset). Exposed for tests/diagnostics. */
  get consumed(): number {
    return this.offset;
  }

  /** Id of the current live socket (bumped on each open). 0 before the first open. A caller can
   *  capture it after one frame and compare before a later frame to detect an intervening reconnect
   *  (#287). */
  get connectionId(): number {
    return this.connId;
  }

  connect(): void {
    this.stopped = false;
    this.handlers.onStatus(
      this.attempt === 0
        ? { kind: "connecting" }
        : { kind: "reconnecting", attempt: this.attempt },
    );
    const ws = this.wsFactory(this.urlFor(this.offset));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    // Watchdog (#236): if the handshake hasn't opened in time, close it so onclose runs the
    // normal retry path in seconds rather than waiting out the browser's ~30s socket timeout.
    this.clearConnectTimer();
    this.connectTimer = setTimeout(() => {
      if (this.ws !== ws) return; // superseded by a newer socket (a wake replaced us)
      if (ws.readyState !== 1) {
        try {
          ws.close();
        } catch {
          /* already closing */
        }
      }
    }, CONNECT_TIMEOUT_MS);
    // Every handler is guarded by socket identity: after the watchdog closes a hung socket, a
    // wake (online/visible) can create + open a replacement before the old socket's late
    // onclose/onerror fires. Without the guard that stale event would clear `this.ws` and
    // schedule a retry, tearing down the live replacement (Hermes #236).
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.clearConnectTimer();
      this.attempt = 0; // a successful open resets the backoff
      this.connId += 1; // a new live socket — callers gate multi-frame sends on this (#287)
      this.handlers.onStatus({ kind: "connected" });
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws !== ws) return;
      this.onMessage(ev.data);
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return;
      this.clearConnectTimer();
      this.onClose(ev.code);
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    };
  }

  private clearConnectTimer(): void {
    if (this.connectTimer) {
      clearTimeout(this.connectTimer);
      this.connectTimer = null;
    }
  }

  /** Online / tab-visible again: if we're idle in backoff (or closed), retry now instead of
   *  waiting out the timer. A healthy in-progress (CONNECTING) or open socket is left alone —
   *  the connect watchdog already bounds a hung handshake. (#236) */
  private wake(): void {
    if (this.stopped || this.rejected) return; // never resurrect a deliberate no-retry reject
    const rs = this.ws?.readyState;
    if (rs === 0 /* CONNECTING */ || rs === 1 /* OPEN */) return;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.attempt = 0;
    this.connect();
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
          holder?: TermGateHolder | null;
          cursor?: number;
        };
        if (msg.t === "seq" && typeof msg.n === "number") {
          this.offset = msg.n;
          this.handlers.onSeq?.(msg.n);
        }
        // {"t":"id","sid":"opencode:ses_…"} — the new-session reconcile result (#127).
        else if (msg.t === "id" && typeof msg.sid === "string")
          this.handlers.onId?.(msg.sid);
        // {"t":"role","role":"owner"|"secondary","holder"?} — per-tab claim verdict
        // (#184/#293/#434). A `secondary` carries the active viewer's `holder` (flag-on
        // take-over) for the read-only banner; the stream keeps flowing either way.
        else if (
          msg.t === "role" &&
          (msg.role === "owner" || msg.role === "secondary")
        )
          this.handlers.onRole?.(msg.role, msg.holder ?? null);
        // {"t":"hist","cursor":N} — exact first-page history cursor of a transcript attach (#348).
        else if (msg.t === "hist" && typeof msg.cursor === "number")
          this.handlers.onHist?.(msg.cursor);
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
      this.rejected = true;
      this.handlers.onStatus({
        kind: "rejected",
        reason: REJECT_REASON[code] ?? "unavailable",
      });
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
    if (typeof window !== "undefined")
      window.removeEventListener("online", this.onOnline);
    if (typeof document !== "undefined")
      document.removeEventListener("visibilitychange", this.onVisible);
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.clearConnectTimer();
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
