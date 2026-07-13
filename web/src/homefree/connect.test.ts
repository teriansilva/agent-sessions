// @vitest-environment node
//
// Drives the DOM-free app-mode connect core against an in-process relay stub
// (using the Responder from handshake.ts) over a linked in-memory socket pair.

import { sha256 } from "@noble/hashes/sha2.js";
import { describe, expect, it } from "vitest";

import { Responder, derivePsk } from "./handshake";
import {
  APP_ADVERT,
  type AltchaChallenge,
  type SocketLike,
  ViewerError,
  runAppSession,
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
      runAppSession(ws, "k", "x", { onFrame: () => {}, onEvent: () => {} }),
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
      runAppSession(clientWs, "k", "x", { onFrame: () => {}, onEvent: () => {} }),
    ).rejects.toMatchObject({ code: "busy" });
  });
});

// A relay + Responder for app mode: pairs, handshakes, records the FIRST decrypted frame
// (must be the advert), then echoes the next frame (+ a marker byte) as an agent→viewer frame.
async function fakeAppRelay(
  ws: MockSocket,
  accessKey: string,
  received: Uint8Array[],
): Promise<void> {
  const chan = makeChannel(ws);
  await chan.opened;
  await chan.recvText(); // hello
  chan.send(JSON.stringify({ t: "paired", deadline: 1_000_000_000, ttl: 14400 }));
  const res = new Responder(await derivePsk(accessKey));
  const msg1 = await chan.recvBinary();
  chan.send(await res.respond(msg1));
  const msg3 = await chan.recvBinary();
  const transport = await res.finish(msg3);
  received.push(await transport.decrypt(await chan.recvBinary())); // 1st frame = advert
  const next = await transport.decrypt(await chan.recvBinary()); // a mux frame from the viewer
  chan.send(await transport.encrypt(new Uint8Array([...next, 0xab]))); // echo back +marker
}

describe("runAppSession", () => {
  it("sends the app advert first, then bridges mux frames both ways", async () => {
    const [clientWs, serverWs] = linkedPair();
    const key = "app-mode-key";
    const received: Uint8Array[] = [];
    const relay = fakeAppRelay(serverWs, key, received);

    const frames: Uint8Array[] = [];
    const handle = await runAppSession(clientWs, key, "captcha", {
      onFrame: (f) => frames.push(f),
      onEvent: () => {},
    });
    handle.sendFrame(new Uint8Array([1, 2, 3])); // a mux frame after the advert
    await relay;
    await new Promise((r) => setTimeout(r, 20)); // let the echo arrive

    expect(received[0]).toEqual(APP_ADVERT); // advert was the FIRST encrypted frame
    expect(frames.at(-1)).toEqual(new Uint8Array([1, 2, 3, 0xab])); // agent frame bridged back
    handle.close();
  });

  it("rejects a wrong access key at the handshake", async () => {
    const [clientWs, serverWs] = linkedPair();
    void fakeAppRelay(serverWs, "correct-key", []).catch(() => {});
    await expect(
      runAppSession(clientWs, "wrong-key", "x", { onFrame: () => {}, onEvent: () => {} }),
    ).rejects.toThrow(); // the Initiator handshake rejects a bad key (generic Error)
  });
});
