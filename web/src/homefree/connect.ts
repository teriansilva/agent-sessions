// Home Free connect flow — the DOM-free, testable core that drives an app-mode
// viewer session against the blind relay: solve the ALTCHA proof-of-work, open
// the viewer WebSocket, run the E2E handshake (Initiator from handshake.ts), and
// bridge encrypted mux frames to/from the tunnel. The DOM wiring lives in
// connect.main.ts.

import { sha256 } from "@noble/hashes/sha2.js";

import { Initiator, derivePsk } from "./handshake";

export class ViewerError extends Error {
  readonly code: string;
  constructor(code: string, message?: string) {
    super(message ?? code);
    this.code = code;
  }
}

export interface AltchaChallenge {
  algorithm: string;
  challenge: string;
  salt: string;
  signature: string;
  maxnumber: number;
}

function toHex(u: Uint8Array): string {
  let s = "";
  for (const b of u) s += b.toString(16).padStart(2, "0");
  return s;
}

/** Solve an ALTCHA challenge: find `number` with sha256(salt+number) == challenge. */
export function solveAltcha(ch: AltchaChallenge): string {
  const enc = new TextEncoder();
  for (let n = 0; n <= ch.maxnumber; n++) {
    if (toHex(sha256(enc.encode(ch.salt + n))) === ch.challenge) {
      return btoa(
        JSON.stringify({
          algorithm: "SHA-256",
          challenge: ch.challenge,
          number: n,
          salt: ch.salt,
          signature: ch.signature,
        }),
      );
    }
  }
  throw new ViewerError("captcha_unsolvable");
}

/** Derive the relay's HTTP + WS endpoints from a base origin (e.g. https://box:4443). */
export function relayUrls(base: string, name: string): { altchaUrl: string; wsUrl: string } {
  const b = base.replace(/\/+$/, "");
  const ws = b.replace(/^http/, "ws");
  return {
    altchaUrl: `${b}/altcha/challenge`,
    wsUrl: `${ws}/relay/ws?role=viewer&name=${encodeURIComponent(name)}`,
  };
}

/** Minimal WebSocket surface used by the session — real WebSocket satisfies it. */
export interface SocketLike {
  binaryType: string;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  readyState?: number;
  send(data: string | ArrayBufferLike | Uint8Array): void;
  close(): void;
}

/** Turns a SocketLike's callbacks into an awaitable frame queue. */
export class FrameChannel {
  private readonly ws: SocketLike;
  private readonly queue: (string | Uint8Array)[] = [];
  private readonly waiters: ((v: string | Uint8Array | null) => void)[] = [];
  private closed = false;
  private isOpen = false;
  private openResolve: (() => void) | null = null;
  private openReject: ((err: Error) => void) | null = null;

  constructor(ws: SocketLike) {
    this.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      this.isOpen = true;
      this.openResolve?.();
    };
    ws.onmessage = (ev) => this.push(this.normalize(ev.data));
    ws.onclose = () => this.finish();
    ws.onerror = () => this.finish();
    if (ws.readyState === 1) this.isOpen = true;
  }

  private normalize(data: unknown): string | Uint8Array {
    if (typeof data === "string") return data;
    if (data instanceof Uint8Array) return data;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    return new Uint8Array(0);
  }

  private push(v: string | Uint8Array): void {
    const w = this.waiters.shift();
    if (w) w(v);
    else this.queue.push(v);
  }

  private finish(): void {
    if (this.closed) return;
    this.closed = true;
    // If the socket failed/closed before it ever opened, settle open() too so a
    // caller awaiting the handshake doesn't hang forever on an unreachable relay.
    if (!this.isOpen) this.openReject?.(new ViewerError("connection_failed"));
    while (this.waiters.length) this.waiters.shift()?.(null);
  }

  open(): Promise<void> {
    if (this.isOpen) return Promise.resolve();
    if (this.closed) return Promise.reject(new ViewerError("connection_failed"));
    return new Promise((resolve, reject) => {
      this.openResolve = resolve;
      this.openReject = reject;
    });
  }

  send(data: string | Uint8Array): void {
    this.ws.send(data);
  }

  close(): void {
    this.ws.close();
  }

  recv(): Promise<string | Uint8Array | null> {
    if (this.queue.length) return Promise.resolve(this.queue.shift() ?? null);
    if (this.closed) return Promise.resolve(null);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  async recvText(): Promise<string> {
    const f = await this.recv();
    if (typeof f !== "string") throw new ViewerError("protocol", "expected a text frame");
    return f;
  }

  async recvBinary(): Promise<Uint8Array> {
    const f = await this.recv();
    if (!(f instanceof Uint8Array)) throw new ViewerError("protocol", "expected a binary frame");
    return f;
  }
}

export type SessionEvent =
  | { type: "paired"; deadline?: number; ttl?: number }
  | { type: "warn"; remaining?: number }
  | { type: "expired" }
  | { type: "closed" };

/** The app-mode advert (#579): the exact first encrypted frame after the E2E handshake.
 *  Must byte-match the agent's `_APP_ADVERT` (`b"\x00HF-APP/1"`). Anything else makes the
 *  agent close the session; there is no recovery-shell fallback. */
export const APP_ADVERT: Uint8Array = new Uint8Array([0, ...new TextEncoder().encode("HF-APP/1")]);

export interface AppSessionCallbacks {
  /** One decrypted mux frame from the box (feed it to the tunnel's `Mux`). */
  onFrame: (frame: Uint8Array) => void;
  onEvent: (evt: SessionEvent) => void;
}

export interface AppSessionHandle {
  /** Encrypt + send one mux frame, serialized (AES-GCM counters stay monotonic). */
  sendFrame: (frame: Uint8Array) => void;
  close: () => void;
}

/**
 * Drive an **app-mode** viewer session (#579): hello + pairing + Initiator handshake, then
 * (1) send the app advert as the first encrypted frame to prove this is an app-capable viewer,
 * and (2) bridge each subsequent encrypted frame to/from a mux via callbacks — so the caller can
 * run the P1 mux + P3 tunnel over it. The relay still only sees ciphertext.
 */
export async function runAppSession(
  ws: SocketLike,
  accessKey: string,
  captcha: string,
  cb: AppSessionCallbacks,
): Promise<AppSessionHandle> {
  const chan = new FrameChannel(ws);
  await chan.open();

  chan.send(JSON.stringify({ t: "hello", captcha }));
  const first = JSON.parse(await chan.recvText());
  if (first.t === "error") throw new ViewerError(first.code ?? "error");
  if (first.t !== "paired") throw new ViewerError("protocol", `expected paired, got ${first.t}`);
  cb.onEvent({ type: "paired", deadline: first.deadline, ttl: first.ttl });

  const ini = new Initiator(await derivePsk(accessKey));
  chan.send(ini.start()); // msg1
  const msg2 = await chan.recvBinary();
  const { transport, msg3 } = await ini.finish(msg2);
  chan.send(msg3);

  let sendChain: Promise<void> = Promise.resolve();
  const sendFrame = (frame: Uint8Array): void => {
    sendChain = sendChain.then(async () => {
      chan.send(await transport.encrypt(frame));
    });
  };
  sendFrame(APP_ADVERT); // request app mode — the FIRST encrypted frame (counter 0)

  void (async () => {
    for (;;) {
      const frame = await chan.recv();
      if (frame == null) {
        cb.onEvent({ type: "closed" });
        break;
      }
      if (typeof frame === "string") {
        const m = JSON.parse(frame);
        if (m.t === "warn") cb.onEvent({ type: "warn", remaining: m.remaining });
        else if (m.t === "expired") {
          cb.onEvent({ type: "expired" });
          break;
        }
      } else {
        cb.onFrame(await transport.decrypt(frame));
      }
    }
  })();

  return { sendFrame, close: () => chan.close() };
}
