// @vitest-environment node
//
// P4b browser app-mode: the WebSocket backstop + the tunnel wiring. Proves an /api request
// round-trips browser → relay → agent-mux → app, and that the global WebSocket shim is scoped
// (only /ws, restored on teardown). The full React mount + session switching is P7 (real browser).

import { describe, expect, it } from "vitest";

import type { SocketLike } from "./connect";
import { Responder, derivePsk } from "./handshake";
import { Mux, type Stream } from "./mux";
import { installWsBackstop, isAppSocketUrl, mountApp, wireAppTunnel } from "./appMount";
import type { Tunnel } from "./tunnel";

const te = new TextEncoder();

interface Sock extends SocketLike {
  _peer: Sock | null;
}
function makeSock(): Sock {
  return {
    binaryType: "arraybuffer",
    readyState: 1,
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
    _peer: null,
    send(data) {
      const peer = this._peer;
      queueMicrotask(() => peer?.onmessage?.({ data }));
    },
    close() {
      const peer = this._peer;
      queueMicrotask(() => {
        this.onclose?.();
        peer?.onclose?.();
      });
    },
  };
}
function pair(): [Sock, Sock] {
  const a = makeSock();
  const b = makeSock();
  a._peer = b;
  b._peer = a;
  return [a, b];
}

// A minimal awaitable channel for the agent (relay-server) side.
function agentChannel(ws: Sock) {
  const q: (string | Uint8Array)[] = [];
  const waiters: ((v: string | Uint8Array | null) => void)[] = [];
  let closed = false;
  ws.onmessage = (ev) => {
    const d = ev.data;
    const v = typeof d === "string" ? d : d instanceof Uint8Array ? d : new Uint8Array(d as ArrayBuffer);
    const w = waiters.shift();
    if (w) w(v);
    else q.push(v);
  };
  ws.onclose = () => {
    closed = true;
    while (waiters.length) waiters.shift()?.(null);
  };
  const recv = (): Promise<string | Uint8Array | null> =>
    q.length ? Promise.resolve(q.shift() ?? null) : closed ? Promise.resolve(null) : new Promise((r) => waiters.push(r));
  return {
    send: (d: string | Uint8Array) => ws.send(d),
    recv,
    recvText: async () => {
      const f = await recv();
      if (typeof f !== "string") throw new Error("expected text");
      return f;
    },
    recvBinary: async () => {
      const f = await recv();
      if (!(f instanceof Uint8Array)) throw new Error("expected binary");
      return f;
    },
  };
}

// The box side: pair + handshake, read the advert, run a responder Mux, and answer every HTTP
// stream with a canned `u32 meta_len | meta_json | body` (mirrors AppProxyTarget's HTTP framing).
async function fakeAppAgent(ws: Sock, accessKey: string, respBody: Uint8Array): Promise<void> {
  const chan = agentChannel(ws);
  await chan.recvText(); // hello
  chan.send(JSON.stringify({ t: "paired", deadline: 1_000_000_000, ttl: 14400 }));
  const res = new Responder(await derivePsk(accessKey));
  chan.send(await res.respond(await chan.recvBinary())); // msg2
  const transport = await res.finish(await chan.recvBinary()); // msg3
  await transport.decrypt(await chan.recvBinary()); // the app advert (first frame)

  let sendChain: Promise<void> = Promise.resolve();
  const mux = new Mux({
    isInitiator: false,
    onSend: (f) => {
      sendChain = sendChain.then(async () => chan.send(await transport.encrypt(f)));
    },
    onStream: (s) => void serveHttp(s, respBody),
  });
  for (;;) {
    const f = await chan.recv();
    if (f == null) break;
    if (f instanceof Uint8Array) mux.feed(await transport.decrypt(f));
  }
}

async function serveHttp(s: Stream, body: Uint8Array): Promise<void> {
  for (;;) {
    const p = await s.read();
    if (p.length === 0) break; // drain the request body
  }
  const meta = te.encode(JSON.stringify({ status: 200, headers: [["content-type", "application/json"]] }));
  const len = new Uint8Array(4);
  new DataView(len.buffer).setUint32(0, meta.length, false);
  await s.write(len);
  await s.write(meta);
  await s.write(body);
  await s.end();
}

describe("isAppSocketUrl", () => {
  it("matches only SAME-ORIGIN /ws paths (external /ws stays off the tunnel)", () => {
    expect(isAppSocketUrl("/ws/term/claude:abc", "box.example")).toBe(true); // relative → same-origin
    expect(isAppSocketUrl("wss://box.example/ws/x", "box.example")).toBe(true); // same-origin absolute
    expect(isAppSocketUrl("wss://external.example/ws/x", "box.example")).toBe(false); // external /ws → NOT app
    expect(isAppSocketUrl("/api/sessions", "box.example")).toBe(false); // not /ws
    expect(isAppSocketUrl("https://box.example/socket", "box.example")).toBe(false); // same-origin non-/ws
    expect(isAppSocketUrl("garbage", "box.example")).toBe(false);
  });
});

describe("installWsBackstop", () => {
  it("routes /ws through the tunnel, external via the real ctor, and restores on teardown", () => {
    const created: string[] = [];
    const fakeTunnel = {
      wsFactory: (u: string) => {
        created.push(u);
        return { tag: "mux" } as unknown as WebSocket;
      },
    } as unknown as Tunnel;
    class RealWS {
      constructor(public url: string | URL) {}
    }
    const win = { WebSocket: RealWS as unknown as typeof WebSocket };
    const restore = installWsBackstop(fakeTunnel, win, "box.example");

    new win.WebSocket("/ws/term/x"); // relative /ws → tunnel
    new win.WebSocket("wss://box.example/ws/x"); // same-origin /ws → tunnel
    const ext = new win.WebSocket("wss://external.example/ws/live"); // external /ws → real ctor
    expect(created).toEqual(["/ws/term/x", "wss://box.example/ws/x"]);
    expect(ext).toBeInstanceOf(RealWS);

    restore();
    expect(win.WebSocket).toBe(RealWS as unknown as typeof WebSocket);
  });
});

describe("wireAppTunnel", () => {
  it("round-trips an /api request browser → relay → agent, and restores WebSocket on teardown", async () => {
    const orig = globalThis.WebSocket;
    const [clientWs, serverWs] = pair();
    const key = "app-mode-key";
    void fakeAppAgent(serverWs, key, te.encode('{"csrf":"tok"}'));

    const wired = await wireAppTunnel(clientWs, key, "captcha");
    expect(globalThis.WebSocket).not.toBe(orig); // scoped backstop installed

    const resp = await wired.tunnel.fetch("/api/config");
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ csrf: "tok" });

    wired.teardown();
    expect(globalThis.WebSocket).toBe(orig); // restored — never outlives the session
  });
});

describe("mountApp", () => {
  it("tears down the tunnel/seams when the mount step fails (no leaked shim/session)", async () => {
    const orig = globalThis.WebSocket;
    const [clientWs, serverWs] = pair();
    void fakeAppAgent(serverWs, "k", te.encode("{}"));
    await expect(
      mountApp(clientWs, "k", "x", {
        render: async () => {
          throw new Error("boom"); // e.g. a stale/blocked app chunk
        },
      }),
    ).rejects.toThrow("boom");
    expect(globalThis.WebSocket).toBe(orig); // WebSocket restored despite the mount failure
  });

  it("mounts on success and teardown unmounts + restores the WebSocket global", async () => {
    const orig = globalThis.WebSocket;
    const [clientWs, serverWs] = pair();
    void fakeAppAgent(serverWs, "k", te.encode("{}"));
    let unmounted = false;
    const mounted = await mountApp(clientWs, "k", "x", {
      render: async () => () => {
        unmounted = true;
      },
    });
    expect(globalThis.WebSocket).not.toBe(orig); // backstop installed while mounted
    mounted.teardown();
    expect(unmounted).toBe(true);
    expect(globalThis.WebSocket).toBe(orig);
  });
});
