// @vitest-environment node
//
// Drives the DOM-free connect core against an in-process relay stub (using the
// Responder from handshake.ts) over a linked in-memory socket pair — proving the
// viewer session completes the handshake and bridges bytes end-to-end.

import { sha256 } from "@noble/hashes/sha2.js";
import { describe, expect, it } from "vitest";

import { Responder, derivePsk } from "./handshake";
import {
  type AltchaChallenge,
  type SessionEvent,
  type SocketLike,
  ViewerError,
  runViewerSession,
  solveAltcha,
} from "./connect";

interface MockSocket extends SocketLike {
  _peer: MockSocket | null;
}

function makeSocket(): MockSocket {
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

function linkedPair(): [MockSocket, MockSocket] {
  const a = makeSocket();
  const b = makeSocket();
  a._peer = b;
  b._peer = a;
  return [a, b];
}

// A tiny relay + Responder that pairs, handshakes, and echoes "echo:<input>".
async function fakeRelay(ws: MockSocket, accessKey: string): Promise<void> {
  const chan = makeChannel(ws);
  await chan.opened;
  await chan.recvText(); // hello (captcha ignored in the stub)
  chan.send(JSON.stringify({ t: "paired", deadline: 1_000_000_000, ttl: 3600 }));
  const res = new Responder(await derivePsk(accessKey));
  const msg1 = await chan.recvBinary();
  chan.send(await res.respond(msg1));
  const msg3 = await chan.recvBinary();
  const transport = await res.finish(msg3);
  for (;;) {
    const f = await chan.recv();
    if (f == null) break;
    if (f instanceof Uint8Array) {
      const pt = await transport.decrypt(f);
      const out = new Uint8Array(5 + pt.length);
      out.set(new TextEncoder().encode("echo:"));
      out.set(pt, 5);
      chan.send(await transport.encrypt(out));
    }
  }
}

// Minimal awaitable channel for the relay side of the test.
function makeChannel(ws: MockSocket) {
  const queue: (string | Uint8Array)[] = [];
  const waiters: ((v: string | Uint8Array | null) => void)[] = [];
  let closed = false;
  ws.binaryType = "arraybuffer";
  ws.onmessage = (ev) => {
    const d = ev.data;
    const v = typeof d === "string" ? d : d instanceof Uint8Array ? d : new Uint8Array(d as ArrayBuffer);
    const w = waiters.shift();
    if (w) w(v);
    else queue.push(v);
  };
  ws.onclose = () => {
    closed = true;
    while (waiters.length) waiters.shift()?.(null);
  };
  const recv = (): Promise<string | Uint8Array | null> => {
    if (queue.length) return Promise.resolve(queue.shift() ?? null);
    if (closed) return Promise.resolve(null);
    return new Promise((r) => waiters.push(r));
  };
  return {
    opened: Promise.resolve(),
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

const waitFor = async (pred: () => boolean, ms = 2000) => {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > ms) throw new Error("waitFor timed out");
    await new Promise((r) => setTimeout(r, 2));
  }
};

describe("Home Free connect core", () => {
  it("solves an ALTCHA challenge and encodes the payload", () => {
    const enc = new TextEncoder();
    // build a challenge with a known answer
    const salt = "abc.9999999999";
    const number = 4242;
    // build the matching challenge hash via the same sha256 the solver uses
    const toHex = (u: Uint8Array) => [...u].map((b) => b.toString(16).padStart(2, "0")).join("");
    const challenge = toHex(sha256(enc.encode(salt + number)));
    const ch: AltchaChallenge = {
      algorithm: "SHA-256",
      challenge,
      salt,
      signature: "sig",
      maxnumber: 10000,
    };
    const payload = JSON.parse(atob(solveAltcha(ch)));
    expect(payload.number).toBe(number);
    expect(payload.challenge).toBe(challenge);
    expect(payload.signature).toBe("sig");
  });

  it("completes the handshake and round-trips bytes through the relay", async () => {
    const key = "AZURE-TEST-KEY-abc123";
    const [clientWs, serverWs] = linkedPair();
    const outputs: Uint8Array[] = [];
    const events: SessionEvent[] = [];
    void fakeRelay(serverWs, key);

    const handle = await runViewerSession(clientWs, key, "captcha-ignored", {
      onOutput: (b) => outputs.push(b),
      onEvent: (e) => events.push(e),
    });
    expect(events.some((e) => e.type === "paired")).toBe(true);

    handle.sendInput(new TextEncoder().encode("whoami\n"));
    await waitFor(() => outputs.length > 0);
    expect(new TextDecoder().decode(outputs[0])).toBe("echo:whoami\n");
    handle.close();
  });

  it("rejects a wrong access key at the handshake", async () => {
    const [clientWs, serverWs] = linkedPair();
    void fakeRelay(serverWs, "correct-key");
    await expect(
      runViewerSession(clientWs, "wrong-key", "x", { onOutput: () => {}, onEvent: () => {} }),
    ).rejects.toThrow();
  });

  it("rejects (does not hang) when the socket fails before opening", async () => {
    // A socket that errors before onopen — e.g. an unreachable relay / bad TLS.
    const ws: SocketLike = {
      binaryType: "",
      readyState: 0,
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      send() {},
      close() {},
    };
    setTimeout(() => ws.onerror?.(), 0);
    await expect(
      runViewerSession(ws, "k", "x", { onOutput: () => {}, onEvent: () => {} }),
    ).rejects.toBeInstanceOf(ViewerError);
  });

  it("surfaces a relay error frame as a ViewerError", async () => {
    const [clientWs, serverWs] = linkedPair();
    // relay stub that rejects with an error instead of pairing
    const chan = makeChannel(serverWs);
    void (async () => {
      await chan.recvText();
      chan.send(JSON.stringify({ t: "error", code: "busy" }));
    })();
    await expect(
      runViewerSession(clientWs, "k", "x", { onOutput: () => {}, onEvent: () => {} }),
    ).rejects.toMatchObject({ code: "busy" });
  });
});
