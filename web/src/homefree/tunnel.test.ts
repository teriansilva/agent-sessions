import { expect, test } from "vitest";

import { type TermStatus, TermSocket } from "../lib/termSocket";
import { Mux, type Stream } from "./mux";
import { type CloseEventLike, createTunnel, MuxWebSocket, type Tunnel } from "./tunnel";

const te = new TextEncoder();
const td = new TextDecoder();

// ---- in-test agent responder: speaks P2's appproxy.py wire over a responder mux ----

interface Agent {
  tunnel: Tunnel;
}

/** Wire a browser tunnel to an agent responder that handles each opened stream. The two
 *  muxes feed each other directly (the E2E crypto is tested in handshake.test.ts). */
function wire(handle: (info: Record<string, unknown>, s: Stream) => Promise<void>): Agent {
  // Holder so `send` can reference the responder mux before it's constructed (no `let`).
  const ref: { agent: Mux | null } = { agent: null };
  const tunnel = createTunnel((f) => ref.agent?.feed(f));
  ref.agent = new Mux({
    isInitiator: false,
    onSend: (f) => tunnel.feed(f),
    onStream: (s) => {
      const info = JSON.parse(td.decode(s.openInfo)) as Record<string, unknown>;
      void handle(info, s);
    },
  });
  return { tunnel };
}

async function drain(s: Stream): Promise<Uint8Array> {
  const chunks: number[] = [];
  for (;;) {
    const p = await s.read();
    if (p.length === 0) break;
    for (const b of p) chunks.push(b);
  }
  return Uint8Array.from(chunks);
}

/** Reply to an HTTP stream with the P2 framing: `u32 meta_len | meta_json | body` + END. */
async function httpReply(
  s: Stream,
  status: number,
  headers: [string, string][],
  body: Uint8Array,
): Promise<void> {
  const meta = te.encode(JSON.stringify({ status, headers }));
  const lenPrefix = new Uint8Array(4);
  new DataView(lenPrefix.buffer).setUint32(0, meta.length, false);
  await s.write(lenPrefix);
  await s.write(meta);
  if (body.length) await s.write(body);
  await s.end();
}

/** A WS message frame as the wire carries it: `type(u8) · len(u32) · payload`. */
function wsFrame(mtype: number, payload: Uint8Array): Uint8Array {
  const out = new Uint8Array(5 + payload.length);
  out[0] = mtype;
  new DataView(out.buffer).setUint32(1, payload.length, false);
  out.set(payload, 5);
  return out;
}

/** An agent→browser CLOSE frame (type 2): `u16 code + utf8 reason`. */
function wsCloseFrame(code: number, reason: string): Uint8Array {
  const r = te.encode(reason);
  const payload = new Uint8Array(2 + r.length);
  new DataView(payload.buffer).setUint16(0, code, false);
  payload.set(r, 2);
  return wsFrame(2, payload);
}

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));
const opened = (ws: MuxWebSocket): Promise<void> =>
  new Promise((res) => {
    ws.onopen = () => res();
  });

// ---------------------------------- tunnelFetch ----------------------------------

test("tunnelFetch round-trips a GET: path + headers to the agent, Response back", async () => {
  let info: Record<string, unknown> = {};
  let body = new Uint8Array(0);
  const { tunnel } = wire(async (i, s) => {
    info = i;
    body = await drain(s);
    await httpReply(s, 200, [["content-type", "application/json"]], te.encode('{"ok":true}'));
  });

  const resp = await tunnel.fetch("/api/sessions", {
    headers: { Cookie: "session=abc", "X-CSRF-Token": "tok" },
    credentials: "same-origin",
  });

  expect(resp.status).toBe(200);
  expect(await resp.json()).toEqual({ ok: true });
  expect(info.k).toBe("http");
  expect(info.method).toBe("GET");
  expect(info.path).toBe("/api/sessions");
  const hdrs = info.headers as Record<string, string>;
  expect(hdrs.cookie).toBe("session=abc"); // preserved (lowercased by the Request)
  expect(hdrs["x-csrf-token"]).toBe("tok");
  expect(body.length).toBe(0); // GET has no body
});

test("tunnelFetch sends a POST body + CSRF and returns the status/body", async () => {
  let info: Record<string, unknown> = {};
  let body = new Uint8Array(0);
  const { tunnel } = wire(async (i, s) => {
    info = i;
    body = await drain(s);
    await httpReply(s, 201, [], te.encode("created"));
  });

  const resp = await tunnel.fetch("/api/sessions/x/metadata", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": "tok" },
    body: JSON.stringify({ project: "p" }),
  });

  expect(resp.status).toBe(201);
  expect(await resp.text()).toBe("created");
  expect(td.decode(body)).toBe('{"project":"p"}');
  expect((info.headers as Record<string, string>)["x-csrf-token"]).toBe("tok");
});

test("tunnelFetch serializes a FormData/multipart upload with its boundary Content-Type", async () => {
  let info: Record<string, unknown> = {};
  let body = new Uint8Array(0);
  const { tunnel } = wire(async (i, s) => {
    info = i;
    body = await drain(s);
    await httpReply(s, 200, [], te.encode('{"path":"/u/pasted","name":"pasted"}'));
  });

  // A string form field (not a Blob) so the serialization is realm-independent in the
  // jsdom/undici test env — the FormData → Request.arrayBuffer() path is identical for a
  // File upload; only jsdom's Blob ↔ undici realm mismatch (not the tunnel) differs there.
  const fd = new FormData();
  fd.append("file", "the-file-bytes");
  const resp = await tunnel.fetch("/api/upload", {
    method: "POST",
    headers: { "X-CSRF-Token": "tok" },
    body: fd,
  });

  expect(resp.status).toBe(200);
  const ct = (info.headers as Record<string, string>)["content-type"];
  expect(ct).toMatch(/^multipart\/form-data; boundary=/); // boundary carried to the agent
  const text = td.decode(body);
  expect(text).toContain('name="file"'); // real multipart body serialized end-to-end
  expect(text).toContain("the-file-bytes");
  expect((info.headers as Record<string, string>)["x-csrf-token"]).toBe("tok");
});

test("tunnelFetch rejects when the agent resets the stream (app down / disallowed)", async () => {
  const { tunnel } = wire(async (_i, s) => {
    s.reset();
  });
  await expect(tunnel.fetch("/api/x")).rejects.toThrow(/reset/);
});

test("tunnelFetch rejects a truncated response (meta length never arrives)", async () => {
  const { tunnel } = wire(async (_i, s) => {
    await drain(s);
    await s.end(); // EOF with no meta at all
  });
  await expect(tunnel.fetch("/api/x")).rejects.toThrow(/meta length/);
});

test("tunnelFetch persists the box Set-Cookie and re-attaches it (session/CSRF continuity)", async () => {
  const seenCookies: (string | undefined)[] = [];
  const { tunnel } = wire(async (info, s) => {
    await drain(s);
    seenCookies.push((info.headers as Record<string, string>).cookie);
    // The first response mints a session cookie (as AUTH_MODE=none does on /api/config).
    const setCookie: [string, string][] =
      seenCookies.length === 1
        ? [["set-cookie", "agent_sessions=SID9; Path=/; HttpOnly; SameSite=Lax"]]
        : [];
    await httpReply(s, 200, setCookie, te.encode("{}"));
  });

  await tunnel.fetch("/api/config", { credentials: "same-origin" });
  await tunnel.fetch("/api/prefs", {
    method: "POST",
    headers: { "X-CSRF-Token": "tok" },
    body: "{}",
  });

  expect(seenCookies[0]).toBeUndefined(); // first request: no cookie yet
  expect(seenCookies[1]).toContain("agent_sessions=SID9"); // jar re-attached it (same session → CSRF holds)
});

test("MuxWebSocket carries the tunnel session cookie on the WS upgrade after login", async () => {
  let wsInfo: Record<string, unknown> = {};
  const { tunnel } = wire(async (info, s) => {
    if (info.k === "http") {
      await drain(s);
      await httpReply(s, 200, [["set-cookie", "agent_sessions=SID9; Path=/"]], te.encode("{}"));
      return;
    }
    wsInfo = info;
    await drain(s);
    await s.end();
  });

  await tunnel.fetch("/api/config"); // mints the session cookie into the tunnel jar
  const ws = tunnel.wsFactory("/ws/term/claude:abc");
  await opened(ws);
  await tick();
  expect((wsInfo.headers as Record<string, string>).cookie).toContain("agent_sessions=SID9");
  ws.close();
});

test("switching sessions reuses the tunnel session cookie — no re-auth (#595 P5)", async () => {
  const wsCookies: (string | undefined)[] = [];
  const { tunnel } = wire(async (info, s) => {
    if (info.k === "http") {
      await drain(s);
      await httpReply(s, 200, [["set-cookie", "agent_sessions=SID9; Path=/"]], te.encode("{}"));
      return;
    }
    wsCookies.push((info.headers as Record<string, string>).cookie);
    await drain(s);
    await s.end();
  });

  await tunnel.fetch("/api/config"); // ConfigContext boot → mints the session (no login prompt)
  for (const sid of ["claude:one", "opencode:two"]) {
    const ws = tunnel.wsFactory(`/ws/term/${sid}`); // open / switch to another session
    await opened(ws);
    await tick();
    ws.close();
    await tick();
  }
  // Both terminal sockets carried the SAME session cookie — the app never re-prompts.
  expect(wsCookies.length).toBe(2);
  expect(wsCookies.every((c) => c?.includes("agent_sessions=SID9"))).toBe(true);
});

// --------------------------------- MuxWebSocket ---------------------------------

test("MuxWebSocket round-trips text + binary messages (echo)", async () => {
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    for (;;) {
      const p = await s.read();
      if (p.length === 0) {
        await s.end();
        break;
      }
      await s.write(p); // byte-for-byte echo preserves framing
    }
  });

  const ws = tunnel.wsFactory("/ws/term/claude:abc");
  ws.binaryType = "arraybuffer";
  await opened(ws);
  const got: (string | ArrayBuffer)[] = [];
  ws.onmessage = (ev) => got.push(ev.data);

  ws.send("hello");
  ws.send(new Uint8Array([0xde, 0xad, 0xbe, 0xef]));
  await tick();

  expect(got[0]).toBe("hello");
  expect(new Uint8Array(got[1] as ArrayBuffer)).toEqual(new Uint8Array([0xde, 0xad, 0xbe, 0xef]));
});

test("MuxWebSocket reassembles coalesced and fragmented frames into whole messages", async () => {
  const big = Uint8Array.from({ length: 40000 }, (_, i) => i & 0xff); // > MAX_CHUNK (16 KB)
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    // Two text frames in ONE write (must not coalesce into one message)…
    await s.write(concat([wsFrame(0, te.encode("aa")), wsFrame(0, te.encode("bb"))]));
    // …and one large binary frame the mux will fragment across DATA frames.
    await s.write(wsFrame(1, big));
    await s.end();
  });

  const ws = tunnel.wsFactory("/ws/x");
  ws.binaryType = "arraybuffer";
  await opened(ws);
  const got: (string | ArrayBuffer)[] = [];
  ws.onmessage = (ev) => got.push(ev.data);
  await tick();
  await tick();

  expect(got[0]).toBe("aa");
  expect(got[1]).toBe("bb");
  expect(new Uint8Array(got[2] as ArrayBuffer)).toEqual(big);
});

test("MuxWebSocket fires a clean onclose when the browser closes (P2 END round-trip)", async () => {
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    await drain(s); // read until the browser half-closes, then end back (P2 behaviour)
    await s.end();
  });

  const ws = tunnel.wsFactory("/ws/x");
  await opened(ws);
  const closed = new Promise<CloseEventLike>((res) => {
    ws.onclose = res;
  });
  ws.close();
  const ev = await closed;
  expect(ev.wasClean).toBe(true);
  expect(ev.code).toBe(1000);
});

test("MuxWebSocket fires a non-clean onclose when the agent resets the WS stream", async () => {
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    await tick();
    s.reset();
  });

  const ws = tunnel.wsFactory("/ws/x");
  await opened(ws);
  const ev = await new Promise<CloseEventLike>((res) => {
    ws.onclose = res;
  });
  expect(ev.wasClean).toBe(false);
  expect(ev.code).toBe(1006);
});

test("MuxWebSocket surfaces a proxied close code (4401) instead of a generic close", async () => {
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    await s.write(wsCloseFrame(4401, "session expired"));
    await s.end();
  });

  const ws = tunnel.wsFactory("/ws/term/x");
  await opened(ws);
  const ev = await new Promise<CloseEventLike>((res) => {
    ws.onclose = res;
  });
  expect(ev.code).toBe(4401);
  expect(ev.reason).toBe("session expired");
});

test("a proxied terminal 4401 reaches TermSocket as a no-retry rejected state (not 1000/1006)", async () => {
  const { tunnel } = wire(async (info, s) => {
    if (info.k !== "ws") return;
    await s.write(wsCloseFrame(4401, "")); // /ws/term deliberate auth reject
    await s.end();
  });

  const statuses: TermStatus[] = [];
  const ts = new TermSocket(
    (have) => `/ws/term/x?have=${have}`,
    { onOutput: () => {}, onStatus: (s) => statuses.push(s) },
    (url) => tunnel.wsFactory(url) as unknown as WebSocket,
  );
  ts.connect();
  await tick();
  await tick();
  await tick();

  // NO_RETRY(4401) → a terminal "rejected" state, and it stays there (no reconnecting).
  expect(statuses.some((s) => s.kind === "rejected")).toBe(true);
  expect(statuses.at(-1)?.kind).toBe("rejected");
  expect(statuses.some((s) => s.kind === "reconnecting")).toBe(false);
  ts.close();
});

// --------------------------------- lifecycle ---------------------------------

test("createTunnel.close() fails in-flight requests so no promise dangles", async () => {
  const { tunnel } = wire(async () => {
    /* never respond — the request hangs until the tunnel tears down */
  });
  const p = tunnel.fetch("/api/hang");
  await tick();
  tunnel.close();
  await expect(p).rejects.toThrow();
});

function concat(parts: Uint8Array[]): Uint8Array {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}
