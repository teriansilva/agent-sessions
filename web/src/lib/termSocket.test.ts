import { beforeEach, describe, expect, test, vi } from "vitest";
import { TermSocket, type TermStatus } from "./termSocket";

// Minimal fake WebSocket: lets a test drive open/message/close and capture sends.
class FakeWS {
  static instances: FakeWS[] = [];
  binaryType = "";
  readyState = 0; // CONNECTING
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWS.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  message(data: unknown) {
    this.onmessage?.({ data });
  }
  drop(code: number) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

function makeSocket() {
  const outputs: Uint8Array[] = [];
  const statuses: TermStatus[] = [];
  const ts = new TermSocket(
    (have) => `/ws/term/claude:abc?have=${have}`,
    { onOutput: (b) => outputs.push(b), onStatus: (s) => statuses.push(s) },
    (u) => new FakeWS(u) as unknown as WebSocket,
  );
  return { ts, outputs, statuses };
}

beforeEach(() => {
  FakeWS.instances = [];
  vi.useRealTimers();
});

test("tracks the consumed byte offset and reconnects with ?have=", () => {
  vi.useFakeTimers();
  const { ts, outputs } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(new Uint8Array([1, 2, 3]).buffer); // 3 bytes
  ws.message(new Uint8Array([4, 5]).buffer); // +2 → 5
  expect(outputs).toHaveLength(2);
  expect(ts.consumed).toBe(5);

  ws.drop(1006); // transient drop → schedule reconnect
  vi.advanceTimersByTime(ts.backoffMs(0));
  // The reconnect URL must carry the consumed offset so the server sends only the delta.
  expect(FakeWS.instances[1].url).toBe("/ws/term/claude:abc?have=5");
});

test("a seq control frame sets the authoritative offset", () => {
  const { ts, outputs } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(new Uint8Array([1, 2]).buffer); // offset 2
  ws.message(JSON.stringify({ t: "seq", n: 1000 })); // server says we're at 1000
  expect(ts.consumed).toBe(1000);
  expect(outputs).toHaveLength(1); // the control frame is NOT terminal output
});

test("malformed / unknown control frames are ignored, never written", () => {
  const { ts, outputs } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message("{not json");
  ws.message(JSON.stringify({ t: "whatever" }));
  expect(outputs).toHaveLength(0);
  expect(ts.consumed).toBe(0);
});

describe("close codes", () => {
  test.each([4401, 4403, 4404, 4500])("deliberate reject %i → no reconnect", (code) => {
    vi.useFakeTimers();
    const { ts, statuses } = makeSocket();
    ts.connect();
    FakeWS.instances[0].drop(code);
    vi.advanceTimersByTime(60_000);
    expect(FakeWS.instances).toHaveLength(1); // never reconnected
    expect(statuses.at(-1)).toMatchObject({ kind: "rejected" });
  });

  test("4409 (busy) reconnects — retries until the master is attachable", () => {
    vi.useFakeTimers();
    const { ts } = makeSocket();
    ts.connect();
    FakeWS.instances[0].drop(4409);
    vi.advanceTimersByTime(ts.backoffMs(0));
    expect(FakeWS.instances).toHaveLength(2);
  });
});

test("reconnect backoff grows and caps, and a successful open resets it", () => {
  vi.useFakeTimers();
  const { ts } = makeSocket();
  expect(ts.backoffMs(0)).toBe(600);
  expect(ts.backoffMs(1)).toBe(1200);
  expect(ts.backoffMs(10)).toBe(10_000); // capped

  ts.connect();
  FakeWS.instances[0].drop(1006);
  vi.advanceTimersByTime(ts.backoffMs(0));
  FakeWS.instances[1].open(); // success resets the attempt counter
  FakeWS.instances[1].drop(1006);
  vi.advanceTimersByTime(ts.backoffMs(0)); // next reconnect uses base delay again
  expect(FakeWS.instances).toHaveLength(3);
});

test("close() stops reconnects for good", () => {
  vi.useFakeTimers();
  const { ts } = makeSocket();
  ts.connect();
  ts.close();
  FakeWS.instances[0].drop(1006);
  vi.advanceTimersByTime(60_000);
  expect(FakeWS.instances).toHaveLength(1);
});

test("send() only writes when the socket is open", () => {
  const { ts } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  expect(ts.send({ t: "i", d: "x" })).toBe(false); // still CONNECTING
  ws.open();
  expect(ts.send({ t: "i", d: "x" })).toBe(true);
  expect(ws.sent).toEqual([JSON.stringify({ t: "i", d: "x" })]);
});
