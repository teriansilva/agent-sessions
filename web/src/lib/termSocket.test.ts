import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
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

const created: TermSocket[] = [];
function makeSocket() {
  const outputs: Uint8Array[] = [];
  const statuses: TermStatus[] = [];
  const ids: string[] = [];
  const ts = new TermSocket(
    (have) => `/ws/term/claude:abc?have=${have}`,
    {
      onOutput: (b) => outputs.push(b),
      onStatus: (s) => statuses.push(s),
      onId: (sid) => ids.push(sid),
    },
    (u) => new FakeWS(u) as unknown as WebSocket,
  );
  created.push(ts);
  return { ts, outputs, statuses, ids };
}

beforeEach(() => {
  FakeWS.instances = [];
  vi.useRealTimers();
});

// Close every socket a test made so its online/visibility listeners don't leak into the next
// test (they'd react to the wake-path test's dispatched `online` event and skew instance counts).
afterEach(() => {
  for (const ts of created) ts.close();
  created.length = 0;
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

test("an id control frame fires onId with the real engine-qualified id (#127)", () => {
  // opencode new-session reconcile: the server pushes {"t":"id","sid":"opencode:ses_…"}
  // when it has discovered the real id. It is NOT terminal output and does not move the
  // byte offset.
  const { ts, outputs, ids } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(new Uint8Array([1, 2]).buffer); // 2 bytes of real output
  ws.message(JSON.stringify({ t: "id", sid: "opencode:ses_realreal0000" }));
  expect(ids).toEqual(["opencode:ses_realreal0000"]);
  expect(outputs).toHaveLength(1); // the id frame is not written to the terminal
  expect(ts.consumed).toBe(2); // and does not advance the resume offset
});

describe("close codes", () => {
  test.each([4401, 4403, 4404, 4500])(
    "deliberate reject %i → no reconnect",
    (code) => {
      vi.useFakeTimers();
      const { ts, statuses } = makeSocket();
      ts.connect();
      FakeWS.instances[0].drop(code);
      vi.advanceTimersByTime(60_000);
      expect(FakeWS.instances).toHaveLength(1); // never reconnected
      expect(statuses.at(-1)).toMatchObject({ kind: "rejected" });
    },
  );

  test("4409 (busy) reconnects — retries until the master is attachable", () => {
    vi.useFakeTimers();
    const { ts } = makeSocket();
    ts.connect();
    FakeWS.instances[0].drop(4409);
    vi.advanceTimersByTime(ts.backoffMs(0));
    expect(FakeWS.instances).toHaveLength(2);
  });

  test("4502 (transient start failure) reconnects with backoff, never terminal (#346)", () => {
    vi.useFakeTimers();
    const { ts, statuses } = makeSocket();
    ts.connect();
    FakeWS.instances[0].drop(4502); // spawn EAGAIN / timeout under resource pressure
    expect(statuses.at(-1)).toMatchObject({ kind: "reconnecting" }); // not "rejected"
    vi.advanceTimersByTime(ts.backoffMs(0));
    expect(FakeWS.instances).toHaveLength(2);
    // Still failing → keeps retrying (capped backoff); the condition is transient.
    FakeWS.instances[1].drop(4502);
    vi.advanceTimersByTime(ts.backoffMs(1));
    expect(FakeWS.instances).toHaveLength(3);
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

test("a hung handshake is closed by the connect watchdog and retried (#236)", () => {
  vi.useFakeTimers();
  const { ts } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  // Never open() — simulate a handshake that hangs (network changed/died). The watchdog should
  // close it well before the browser's ~30s socket timeout.
  vi.advanceTimersByTime(8_000);
  expect(ws.readyState).toBe(3); // watchdog closed the hung socket
  ws.onclose?.({ code: 1006 }); // the browser then fires close → normal retry path
  vi.advanceTimersByTime(ts.backoffMs(0));
  expect(FakeWS.instances).toHaveLength(2); // retried in seconds, not ~30s
});

test("coming back online reconnects immediately without waiting out the backoff (#236)", () => {
  vi.useFakeTimers();
  const { ts } = makeSocket();
  ts.connect();
  FakeWS.instances[0].drop(1006); // transient drop → now idle in backoff
  expect(FakeWS.instances).toHaveLength(1);
  window.dispatchEvent(new Event("online")); // network is back
  expect(FakeWS.instances).toHaveLength(2); // immediate retry — no timer advance needed
  void ts; // closed by afterEach
});

test("a wake-created socket survives the previous (watchdog-closed) socket's late close (#236)", () => {
  vi.useFakeTimers();
  const { ts } = makeSocket();
  ts.connect();
  const ws1 = FakeWS.instances[0];
  vi.advanceTimersByTime(8_000); // watchdog closes the hung ws1 (its onclose hasn't fired yet)
  expect(ws1.readyState).toBe(3);
  window.dispatchEvent(new Event("online")); // wake → replacement socket
  const ws2 = FakeWS.instances[1];
  ws2.open();
  expect(ts.send({ t: "i", d: "x" })).toBe(true); // ws2 is the live socket
  ws1.onclose?.({ code: 1006 }); // ws1's late close arrives — must be IGNORED (stale identity)
  expect(ts.send({ t: "i", d: "x" })).toBe(true); // ws2 was NOT torn down
  expect(FakeWS.instances).toHaveLength(2); // and no spurious extra reconnect was scheduled
});

test.each([4401, 4403, 4404, 4500])(
  "a deliberate no-retry %i reject is not resurrected by an online wake (#236)",
  (code) => {
    vi.useFakeTimers();
    const { ts, statuses } = makeSocket();
    ts.connect();
    FakeWS.instances[0].drop(code); // server's deliberate reject → terminal, no retry
    expect(statuses.at(-1)).toMatchObject({ kind: "rejected" });
    expect(FakeWS.instances).toHaveLength(1);
    window.dispatchEvent(new Event("online")); // a wake must NOT undo the deliberate reject
    expect(FakeWS.instances).toHaveLength(1);
  },
);

test("send() only writes when the socket is open", () => {
  const { ts } = makeSocket();
  ts.connect();
  const ws = FakeWS.instances[0];
  expect(ts.send({ t: "i", d: "x" })).toBe(false); // still CONNECTING
  ws.open();
  expect(ts.send({ t: "i", d: "x" })).toBe(true);
  expect(ws.sent).toEqual([JSON.stringify({ t: "i", d: "x" })]);
});

test("a role control frame fires onRole with role + holder, null holder when absent (#434)", () => {
  const roles: Array<[unknown, unknown]> = [];
  const ts = new TermSocket(
    () => "/ws/term/claude:abc?have=0",
    {
      onOutput: () => {},
      onStatus: () => {},
      onRole: (r, h) => roles.push([r, h ?? null]),
    },
    (u) => new FakeWS(u) as unknown as WebSocket,
  );
  created.push(ts);
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  // Flag-on take-over: a read-only secondary carries the active viewer for the banner.
  ws.message(
    JSON.stringify({
      t: "role",
      role: "secondary",
      holder: { label: "Mac · Chrome", since: 1700 },
    }),
  );
  ws.message(JSON.stringify({ t: "role", role: "owner" })); // #184 path → no holder → null
  expect(roles).toEqual([
    ["secondary", { label: "Mac · Chrome", since: 1700 }],
    ["owner", null],
  ]);
});

test("a hist control frame fires onHist with the exact attach cursor (#348)", () => {
  const cursors: number[] = [];
  const ts = new TermSocket(
    () => "/ws/term/claude:abc?have=0",
    { onOutput: () => {}, onStatus: () => {}, onHist: (c) => cursors.push(c) },
    (u) => new FakeWS(u) as unknown as WebSocket,
  );
  created.push(ts);
  ts.connect();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(JSON.stringify({ t: "seq", n: 123 }));
  ws.message(JSON.stringify({ t: "hist", cursor: 7 }));
  ws.message(JSON.stringify({ t: "hist" })); // malformed (no cursor) → ignored
  expect(cursors).toEqual([7]);
  expect(ts.consumed).toBe(123); // the hist frame never disturbs the seq offset
});
