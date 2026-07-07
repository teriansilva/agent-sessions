// Browser mirror of the Home Free stream multiplexer (agent side: mux.py) — #579 P1.
//
// The E2E `Transport` (handshake.ts) is one ordered, reliable, encrypted byte
// channel. To carry the box's whole app — many concurrent `/api` requests plus
// the terminal `/ws` — over it, we multiplex independent **streams**. The mux is
// transport-agnostic: it emits opaque frame bytes via `onSend` and consumes them
// via `feed`, so the relay still only ever sees ciphertext (no relay change).
//
// Wire (must match mux.py + mux.vectors.json exactly):
//   stream_id(u32) · type(u8) · len(u24) · payload[len]
// Types: OPEN=1, DATA=2, END=3 (half-close), RESET=4 (abort), WINDOW=5 (credit).
//
// Flow control is credit-based per stream: a receiver advertises INITIAL_WINDOW
// and only replenishes (WINDOW) as its consumer reads, so an un-read stream
// drains the sender's window and applies real backpressure. Stream ids follow the
// HTTP/2 convention — the initiator opens odd ids, the responder even.

export const OPEN = 1;
export const DATA = 2;
export const END = 3;
export const RESET = 4;
export const WINDOW = 5;

export const INITIAL_WINDOW = 256 * 1024;
export const MAX_CHUNK = 16 * 1024;
const U24_MAX = (1 << 24) - 1;

export function encodeFrame(streamId: number, ftype: number, payload: Uint8Array = new Uint8Array(0)): Uint8Array {
  if (payload.length > U24_MAX) throw new Error("mux frame payload too large");
  const out = new Uint8Array(8 + payload.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, streamId >>> 0, false);
  out[4] = ftype & 0xff;
  out[5] = (payload.length >>> 16) & 0xff;
  out[6] = (payload.length >>> 8) & 0xff;
  out[7] = payload.length & 0xff;
  out.set(payload, 8);
  return out;
}

export function decodeFrame(frame: Uint8Array): { streamId: number; ftype: number; payload: Uint8Array } {
  if (frame.length < 8) throw new Error("short mux frame");
  const dv = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  const streamId = dv.getUint32(0, false);
  const ftype = frame[4];
  const length = (frame[5] << 16) | (frame[6] << 8) | frame[7];
  if (frame.length - 8 !== length) throw new Error("truncated mux frame");
  return { streamId, ftype, payload: frame.subarray(8, 8 + length) };
}

export class StreamReset extends Error {
  readonly code: number;
  constructor(code = 0) {
    super(`stream reset (code ${code})`);
    this.code = code;
  }
}

// A one-shot resettable notifier — the TS analogue of asyncio.Event, used to
// wake a pending read()/write() when data/window/eof/reset arrives.
class Gate {
  private _p!: Promise<void>;
  private _res!: () => void;
  constructor() {
    this.reset();
  }
  reset(): void {
    this._p = new Promise((r) => (this._res = r));
  }
  wait(): Promise<void> {
    return this._p;
  }
  set(): void {
    this._res();
  }
}

export class Stream {
  readonly id: number;
  readonly openInfo: Uint8Array;
  private _mux: Mux;
  private _sendWindow: number;
  private _sendWaiters: Array<() => void> = [];
  private _sendEnded = false;
  private _recvBuf: number[] = [];
  private _recvEof = false;
  private _recvReset: number | null = null;
  private _dataReady = new Gate();

  constructor(mux: Mux, id: number, openInfo: Uint8Array) {
    this._mux = mux;
    this.id = id;
    this.openInfo = openInfo;
    this._sendWindow = mux.initialWindow;
  }

  async write(data: Uint8Array): Promise<void> {
    if (this._sendEnded) throw new StreamReset(0);
    let off = 0;
    while (off < data.length) {
      while (this._sendWindow <= 0) {
        if (this._recvReset !== null) throw new StreamReset(this._recvReset);
        await new Promise<void>((r) => this._sendWaiters.push(r));
      }
      const n = Math.min(data.length - off, this._sendWindow, this._mux.maxChunk);
      this._mux._emit(encodeFrame(this.id, DATA, data.subarray(off, off + n)));
      this._sendWindow -= n;
      off += n;
    }
  }

  async end(): Promise<void> {
    if (!this._sendEnded) {
      this._sendEnded = true;
      this._mux._emit(encodeFrame(this.id, END));
    }
  }

  reset(code = 0): void {
    this._mux._emit(encodeFrame(this.id, RESET, new Uint8Array([code & 0xff])));
    this._mux._drop(this.id);
    this._fail(code);
  }

  /** Up to `maxBytes` of received data; empty array at clean EOF. Reading replenishes the peer window. */
  async read(maxBytes = MAX_CHUNK): Promise<Uint8Array> {
    while (this._recvBuf.length === 0 && !this._recvEof && this._recvReset === null) {
      this._dataReady.reset();
      await this._dataReady.wait();
    }
    if (this._recvReset !== null && this._recvBuf.length === 0) throw new StreamReset(this._recvReset);
    if (this._recvBuf.length === 0) return new Uint8Array(0); // clean EOF
    const n = Math.min(maxBytes, this._recvBuf.length);
    const out = Uint8Array.from(this._recvBuf.splice(0, n));
    const credit = new Uint8Array(4);
    new DataView(credit.buffer).setUint32(0, n, false);
    this._mux._emit(encodeFrame(this.id, WINDOW, credit)); // replenish
    return out;
  }

  _onData(payload: Uint8Array): void {
    for (const b of payload) this._recvBuf.push(b);
    this._dataReady.set();
  }
  _onEnd(): void {
    this._recvEof = true;
    this._dataReady.set();
  }
  _onWindow(credit: number): void {
    this._sendWindow += credit;
    while (this._sendWaiters.length && this._sendWindow > 0) this._sendWaiters.shift()!();
  }
  _fail(code: number): void {
    this._recvReset = code;
    this._dataReady.set();
    while (this._sendWaiters.length) this._sendWaiters.shift()!();
  }
}

export interface MuxOptions {
  isInitiator: boolean;
  onSend: (frame: Uint8Array) => void;
  onStream?: (s: Stream) => void;
  initialWindow?: number;
  maxChunk?: number;
}

export class Mux {
  readonly initialWindow: number;
  readonly maxChunk: number;
  private _onSend: (f: Uint8Array) => void;
  private _onStream?: (s: Stream) => void;
  private _streams = new Map<number, Stream>();
  private _nextId: number;
  private _closed = false;

  constructor(opts: MuxOptions) {
    this._onSend = opts.onSend;
    this._onStream = opts.onStream;
    this._nextId = opts.isInitiator ? 1 : 2;
    this.initialWindow = opts.initialWindow ?? INITIAL_WINDOW;
    this.maxChunk = opts.maxChunk ?? MAX_CHUNK;
  }

  open(openInfo: Uint8Array = new Uint8Array(0)): Stream {
    const sid = this._nextId;
    this._nextId += 2;
    const s = new Stream(this, sid, openInfo);
    this._streams.set(sid, s);
    this._emit(encodeFrame(sid, OPEN, openInfo));
    return s;
  }

  feed(frame: Uint8Array): void {
    if (this._closed) return;
    const { streamId, ftype, payload } = decodeFrame(frame);
    if (ftype === OPEN) {
      const s = new Stream(this, streamId, payload.slice());
      this._streams.set(streamId, s);
      this._onStream?.(s);
      return;
    }
    const s = this._streams.get(streamId);
    if (!s) return; // unknown/closed stream — ignore
    if (ftype === DATA) s._onData(payload);
    else if (ftype === END) s._onEnd();
    else if (ftype === WINDOW) {
      // WINDOW carries a u32 credit — a wrong-length payload is a malformed
      // control frame; abort the stream rather than throw or inflate credit.
      if (payload.length !== 4) s.reset();
      else s._onWindow(new DataView(payload.buffer, payload.byteOffset, payload.byteLength).getUint32(0, false));
    } else if (ftype === RESET) {
      this._drop(streamId);
      s._fail(payload.length ? payload[0] : 0);
    }
  }

  close(): void {
    this._closed = true;
    for (const s of this._streams.values()) s._fail(0);
    this._streams.clear();
  }

  _emit(frame: Uint8Array): void {
    if (!this._closed) this._onSend(frame);
  }
  _drop(sid: number): void {
    this._streams.delete(sid);
  }
}
