// Browser transport adapter — #579 P3. Turns the app's own network calls into mux
// streams (P1) so the real BattleLab SPA's private traffic (`/api` + terminal `/ws`)
// rides the blind E2E relay to the box, where P2's `AppProxyTarget` reverse-proxies
// each stream to the local app. The app code never learns it's tunneled: `api.ts`
// takes an injected `fetch` (`setApiFetch`) and `termSocket.ts` takes an injected
// `wsFactory` — both satisfied here.
//
// The mux rides *inside* the already-encrypted E2E transport (handshake.ts), so the
// relay/edge still only ever see ciphertext — blindness is preserved (no relay change).
//
// Sub-protocol (must match appproxy.py exactly):
//   HTTP  open-info {"k":"http","method","path","headers"}; body over DATA; END closes
//         the request. Response = `u32 meta_len | meta_json | body…` then END, where
//         meta_json = {"status": int, "headers": [[k,v],…]}.
//   WS    open-info {"k":"ws","path","headers"}; each message length-framed as
//         `type(u8) · len(u32) · payload` (0 = text, 1 = binary), both directions. The
//         mux stream is a *byte* stream (DATA coalesces/fragments), so messages MUST be
//         length-delimited, never read()-boundary-delimited.

import { Mux, type Stream, StreamReset } from "./mux";

const te = new TextEncoder();
const td = new TextDecoder();

export type TunnelFetch = (input: string, init?: RequestInit) => Promise<Response>;

/** Read exactly `n` bytes from the mux byte stream, or `null` at clean EOF before `n`. */
async function readExact(s: Stream, n: number): Promise<Uint8Array | null> {
  const buf = new Uint8Array(n);
  let off = 0;
  while (off < n) {
    const part = await s.read(n - off);
    if (part.length === 0) return null; // EOF before we had `n` bytes
    buf.set(part, off);
    off += part.length;
  }
  return buf;
}

function concat(chunks: Uint8Array[]): Uint8Array<ArrayBuffer> {
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

/** The path+query to send in open-info: a plain absolute request path (never a host). */
function toPath(input: string): string {
  if (input.startsWith("/")) return input;
  try {
    const u = new URL(input, "http://app.local");
    return u.pathname + u.search;
  } catch {
    return input;
  }
}

/** ws://host/path or /path → the absolute request path the agent proxies (never a host). */
function toWsPath(url: string): string {
  if (url.startsWith("/")) return url;
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return url;
  }
}

function asBytes(data: ArrayBufferLike | ArrayBufferView): Uint8Array {
  if (data instanceof Uint8Array) return data;
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  return new Uint8Array(data as ArrayBufferLike);
}

/**
 * A tunnel-owned cookie jar. The box's app session cookie (`agent_sessions`, `HttpOnly`)
 * is set by the box's own origin *behind* the tunnel, so the viewer's browser never sees
 * a real `Set-Cookie` and can't hold it (the connect page is a different origin, and the
 * cookie is `HttpOnly` anyway). So the tunnel captures `Set-Cookie` off each proxied
 * response and re-attaches `Cookie` on every subsequent HTTP request + WS upgrade — this
 * is what keeps the session (and thus the body-delivered CSRF token, which is bound to
 * that session) continuous across `/api/config` → mutations → `/ws/term`. Minimal by
 * design: one box / one origin, so no path/domain scoping — just name→value.
 */
class CookieJar {
  private jar = new Map<string, string>();

  /** Store the name=value of each `Set-Cookie` (attributes after the first `;` are dropped). */
  ingest(setCookies: string[]): void {
    for (const sc of setCookies) {
      const first = sc.split(";", 1)[0];
      const eq = first.indexOf("=");
      if (eq <= 0) continue;
      const name = first.slice(0, eq).trim();
      if (name) this.jar.set(name, first.slice(eq + 1).trim());
    }
  }

  /** The `Cookie` header value, merging any caller-supplied cookies (jar wins on conflict). */
  header(existing?: string): string {
    const merged = new Map<string, string>();
    for (const part of (existing ?? "").split(";")) {
      const p = part.trim();
      const i = p.indexOf("=");
      if (i > 0) merged.set(p.slice(0, i).trim(), p.slice(i + 1).trim());
    }
    for (const [k, v] of this.jar) merged.set(k, v);
    return [...merged].map(([k, v]) => `${k}=${v}`).join("; ");
  }

  get size(): number {
    return this.jar.size;
  }
}

/** A `fetch`-compatible function that proxies one `/api` request over one HTTP mux stream. */
function makeTunnelFetch(mux: Mux, jar: CookieJar): TunnelFetch {
  return async (input, init) => {
    // Normalize via a Request so every body kind serializes uniformly — JSON strings,
    // Blobs, and FormData/multipart (whose boundary Content-Type the Request sets and we
    // forward). `arrayBuffer()` yields the exact bytes matching that Content-Type.
    const path = toPath(input);
    const req = new Request(path.startsWith("/") ? `http://app.local${path}` : input, init);
    const headers: Record<string, string> = {};
    req.headers.forEach((v, k) => {
      headers[k] = v;
    });
    // Attach the tunnel's session cookie (the browser can't — it's HttpOnly + cross-origin).
    const cookie = jar.header(headers.cookie);
    if (cookie) headers.cookie = cookie;
    const body = new Uint8Array(await req.arrayBuffer());
    const s = mux.open(te.encode(JSON.stringify({ k: "http", method: req.method, path, headers })));
    try {
      if (body.length) await s.write(body);
      await s.end();
      const lenBuf = await readExact(s, 4);
      if (!lenBuf) throw new TypeError("tunnel: response ended before meta length");
      const metaLen = new DataView(lenBuf.buffer, lenBuf.byteOffset, 4).getUint32(0, false);
      const metaBuf = await readExact(s, metaLen);
      if (!metaBuf) throw new TypeError("tunnel: response ended inside meta");
      const meta = JSON.parse(td.decode(metaBuf)) as {
        status: number;
        headers?: [string, string][];
      };
      const chunks: Uint8Array[] = [];
      for (;;) {
        const part = await s.read();
        if (part.length === 0) break;
        chunks.push(part);
      }
      const respHeaders = new Headers();
      const setCookies: string[] = [];
      for (const [k, v] of meta.headers ?? []) {
        if (k.toLowerCase() === "set-cookie") setCookies.push(v);
        try {
          respHeaders.append(k, v);
        } catch {
          /* skip a header the Headers guard rejects (e.g. a forbidden name) */
        }
      }
      // Capture the box session cookie so subsequent requests + the terminal WS carry it.
      if (setCookies.length) jar.ingest(setCookies);
      // 204/205/304 must carry a null body per the Fetch spec (Response throws otherwise).
      const nullBody = meta.status === 204 || meta.status === 205 || meta.status === 304;
      return new Response(nullBody ? null : concat(chunks), {
        status: meta.status,
        headers: respHeaders,
      });
    } catch (e) {
      // A reset mid-request (app down, disallowed path, transport closed) surfaces as a
      // network-style failure — the same shape a real `fetch` rejects with.
      if (e instanceof StreamReset) throw new TypeError("tunnel: stream reset", { cause: e });
      throw e;
    }
  };
}

// WebSocket.readyState values (kept as literals so this works where the global
// WebSocket constructor isn't defined, e.g. the jsdom/node test env).
const CONNECTING = 0;
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

// WS message-frame types (the `type` byte of `type·len·payload`). 0/1 are bidirectional
// data; 2 is an agent→browser CLOSE carrying the app WebSocket's close code (u16 BE +
// optional UTF-8 reason) — the browser never sends it (it half-closes via `stream.end()`).
const WS_CLOSE = 2;

type MsgEvent = { data: string | ArrayBuffer };
export type CloseEventLike = { code: number; reason: string; wasClean: boolean };

/**
 * A `WebSocket`-shaped adapter over one WS mux stream. Implements exactly the surface
 * `termSocket.ts` (and `connect.ts`'s `SocketLike`) use, so it drops into the existing
 * `wsFactory` seam with no change to `TermSocket`'s resume/backoff logic. Messages are
 * length-framed (`type·len·payload`) so coalesced/fragmented mux DATA reassemble whole.
 */
export class MuxWebSocket {
  binaryType: "blob" | "arraybuffer" = "blob";
  onopen: ((ev?: unknown) => void) | null = null;
  onmessage: ((ev: MsgEvent) => void) | null = null;
  onclose: ((ev: CloseEventLike) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  readyState: number = CONNECTING;

  static readonly CONNECTING = CONNECTING;
  static readonly OPEN = OPEN;
  static readonly CLOSING = CLOSING;
  static readonly CLOSED = CLOSED;

  private readonly stream: Stream;
  private closedFired = false;
  private recv: number[] = [];

  constructor(mux: Mux, url: string, headers: Record<string, string> = {}) {
    this.stream = mux.open(
      te.encode(JSON.stringify({ k: "ws", path: toWsPath(url), headers })),
    );
    // Fire `onopen` on a microtask so the caller can attach handlers after the factory
    // returns (real WebSocket semantics — handlers are set on the returned instance).
    queueMicrotask(() => {
      if (this.closedFired) return;
      this.readyState = OPEN;
      this.onopen?.();
      void this.readLoop();
    });
  }

  send(data: string | ArrayBufferLike | ArrayBufferView): void {
    if (this.readyState !== OPEN) return; // mirror WebSocket: drop sends when not open
    const isText = typeof data === "string";
    const payload = isText ? te.encode(data) : asBytes(data as ArrayBufferLike | ArrayBufferView);
    const frame = new Uint8Array(5 + payload.length);
    frame[0] = isText ? 0 : 1;
    new DataView(frame.buffer).setUint32(1, payload.length, false);
    frame.set(payload, 5);
    void this.stream.write(frame).catch((e) => {
      this.onerror?.(e);
      this.fireClose(1006, "send failed", false);
    });
  }

  close(): void {
    if (this.closedFired || this.readyState === CLOSING) return;
    this.readyState = CLOSING;
    // Half-close our send side; P2 replies with a mux END, so `readLoop` sees clean EOF
    // and fires a clean `onclose`. (A never-opened socket just resets.)
    void this.stream.end().catch(() => {
      /* already ended/reset */
    });
  }

  private async readLoop(): Promise<void> {
    try {
      for (;;) {
        const part = await this.stream.read();
        if (part.length === 0) {
          this.fireClose(1000, "", true); // clean EOF from the peer (P2 END)
          return;
        }
        for (const b of part) this.recv.push(b);
        // Drain every complete `type·len·payload` frame currently buffered.
        for (;;) {
          if (this.recv.length < 5) break;
          const len =
            ((this.recv[1] << 24) | (this.recv[2] << 16) | (this.recv[3] << 8) | this.recv[4]) >>> 0;
          if (this.recv.length < 5 + len) break;
          const mtype = this.recv[0];
          const payload = Uint8Array.from(this.recv.slice(5, 5 + len));
          this.recv.splice(0, 5 + len);
          if (mtype === WS_CLOSE) {
            // The agent serialized the app WebSocket's deliberate close code (u16 BE +
            // optional UTF-8 reason) so TermSocket's NO_RETRY codes (4401/4403/4404/4500)
            // surface as a rejected state instead of an endless reconnect. A close frame
            // is a completed handshake → wasClean.
            const code = payload.length >= 2 ? ((payload[0] << 8) | payload[1]) : 1005;
            const reason = payload.length > 2 ? td.decode(payload.subarray(2)) : "";
            this.fireClose(code, reason, true);
            return;
          }
          this.dispatch(mtype, payload);
        }
      }
    } catch (e) {
      this.onerror?.(e);
      this.fireClose(1006, "stream reset", false);
    }
  }

  private dispatch(mtype: number, payload: Uint8Array): void {
    if (!this.onmessage) return;
    if (mtype === 0) {
      this.onmessage({ data: td.decode(payload) });
    } else {
      // Binary — hand back a fresh ArrayBuffer (termSocket sets binaryType="arraybuffer"
      // and checks `data instanceof ArrayBuffer`). Copying detaches it from the recv buffer.
      this.onmessage({ data: new Uint8Array(payload).buffer });
    }
  }

  private fireClose(code: number, reason: string, wasClean: boolean): void {
    if (this.closedFired) return;
    this.closedFired = true;
    this.readyState = CLOSED;
    this.onclose?.({ code, reason, wasClean });
  }
}

export interface Tunnel {
  /** The browser-side mux (initiator). Streams ride inside the E2E transport. */
  readonly mux: Mux;
  /** Feed one decrypted E2E transport message (exactly one mux frame) into the mux. */
  feed(frame: Uint8Array): void;
  /** A `fetch` for `api.ts`'s `setApiFetch`. */
  readonly fetch: TunnelFetch;
  /** A `wsFactory` for `termSocket.ts` — opens a WS mux stream per terminal socket. */
  wsFactory(url: string): MuxWebSocket;
  /** Tear down: fail every open stream so no in-flight fetch/WS promise dangles. */
  close(): void;
}

/**
 * Build the browser tunnel over an E2E send function. `send(frame)` should encrypt and
 * push one frame to the relay; the connect loop calls `feed(frame)` for each decrypted
 * inbound message. Sends are serialized so the E2E AES-GCM counters stay monotonic even
 * under a burst of concurrent streams.
 */
export function createTunnel(
  send: (frame: Uint8Array) => void | Promise<void>,
  opts: { wsHeaders?: Record<string, string> } = {},
): Tunnel {
  let chain: Promise<void> = Promise.resolve();
  const jar = new CookieJar();
  const mux = new Mux({
    isInitiator: true,
    onSend: (frame) => {
      // Copy defensively — the caller may hold the frame past this synchronous emit.
      const f = frame.slice();
      chain = chain.then(() => send(f)).catch(() => {
        /* a failed send tears the transport down elsewhere; don't unhandled-reject here */
      });
    },
  });
  return {
    mux,
    feed(frame) {
      try {
        mux.feed(frame);
      } catch {
        // A malformed frame off the authenticated transport is corruption, not a normal
        // event — drop it rather than crash the whole tunnel.
      }
    },
    fetch: makeTunnelFetch(mux, jar),
    wsFactory(url) {
      // Carry the tunnel session cookie onto the WS upgrade — /ws/term rejects (4401)
      // without it, and the browser can't supply it (HttpOnly + cross-origin).
      const headers = { ...(opts.wsHeaders ?? {}) };
      const cookie = jar.header(headers.cookie ?? headers.Cookie);
      if (cookie) headers.cookie = cookie;
      return new MuxWebSocket(mux, url, headers);
    },
    close() {
      mux.close(); // fails all open streams → pending read()/write() reject
    },
  };
}
