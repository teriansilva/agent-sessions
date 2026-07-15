// @vitest-environment node
//
// Browser mux (Home Free full-app tunnel, #579 P1) — framing, cross-impl wire
// vectors (must match mux.py), concurrent streams, backpressure, reset.

import { describe, expect, it } from "vitest";

import vectors from "./mux.vectors.json";
import {
  DATA,
  Mux,
  Stream,
  StreamReset,
  WINDOW,
  decodeFrame,
  encodeFrame,
} from "./mux";

const fromHex = (h: string) => new Uint8Array((h.match(/../g) ?? []).map((x) => parseInt(x, 16)));
const toHex = (u: Uint8Array) => [...u].map((b) => b.toString(16).padStart(2, "0")).join("");
const enc = (s: string) => new TextEncoder().encode(s);
const tick = () => new Promise((r) => setTimeout(r, 0));

interface Link {
  a: Mux;
  b: Mux;
  opened: { a: Stream[]; b: Stream[] };
}
function link(opts: { initialWindow?: number; maxChunk?: number } = {}): Link {
  const opened: { a: Stream[]; b: Stream[] } = { a: [], b: [] };
  const mux: { a?: Mux; b?: Mux } = {};
  mux.a = new Mux({ isInitiator: true, onSend: (f) => mux.b!.feed(f), onStream: (s) => opened.a.push(s), ...opts });
  mux.b = new Mux({ isInitiator: false, onSend: (f) => mux.a!.feed(f), onStream: (s) => opened.b.push(s), ...opts });
  return { a: mux.a, b: mux.b, opened };
}

describe("Home Free mux", () => {
  it("matches the cross-impl wire vectors verbatim", () => {
    for (const v of vectors.frames) {
      const frame = encodeFrame(v.stream_id, v.type, fromHex(v.payload_hex));
      expect(toHex(frame)).toBe(v.frame_hex);
      const d = decodeFrame(fromHex(v.frame_hex));
      expect([d.streamId, d.ftype, toHex(d.payload)]).toEqual([v.stream_id, v.type, v.payload_hex]);
    }
  });

  it("rejects a truncated or overlong frame", () => {
    expect(() => decodeFrame(new Uint8Array([0, 0, 0, 1]))).toThrow();
    expect(() => decodeFrame(encodeFrame(1, DATA, enc("abcd")).subarray(0, -1))).toThrow();
    const overlong = new Uint8Array([...encodeFrame(1, DATA, enc("abcd")), 122]); // trailing byte
    expect(() => decodeFrame(overlong)).toThrow();
  });

  it("aborts a stream on a malformed WINDOW frame", async () => {
    for (const bad of [new Uint8Array(2), new Uint8Array(6)]) {
      const { a } = link();
      const s = a.open(enc("w"));
      a.feed(encodeFrame(s.id, WINDOW, bad)); // wrong-length credit → abort, not crash/inflate
      await expect(s.read()).rejects.toBeInstanceOf(StreamReset);
    }
  });

  it("round-trips a stream and signals EOF", async () => {
    const { a, opened } = link();
    const s = a.open(enc("GET /api/x"));
    await s.write(enc("chunk-one"));
    await s.write(enc("chunk-two"));
    await s.end();
    expect(opened.b.length).toBe(1);
    const rs = opened.b[0];
    expect(toHex(rs.openInfo)).toBe(toHex(enc("GET /api/x")));
    let got = "";
    for (;;) {
      const part = await rs.read();
      if (part.length === 0) break;
      got += new TextDecoder().decode(part);
    }
    expect(got).toBe("chunk-onechunk-two");
  });

  it("keeps concurrent streams independent", async () => {
    const { a, opened } = link();
    const s1 = a.open(enc("one"));
    const s2 = a.open(enc("two"));
    await s1.write(enc("AAA"));
    await s2.write(enc("BBB"));
    await s1.write(enc("aaa"));
    const by = new Map(opened.b.map((s) => [new TextDecoder().decode(s.openInfo), s]));
    expect([...by.keys()].sort()).toEqual(["one", "two"]);
    expect(new TextDecoder().decode(await by.get("one")!.read(1024))).toBe("AAAaaa");
    expect(new TextDecoder().decode(await by.get("two")!.read(1024))).toBe("BBB");
  });

  it("applies backpressure until the reader drains", async () => {
    const { a, opened } = link({ initialWindow: 4096, maxChunk: 1024 });
    const s = a.open(enc("bp"));
    const rs = opened.b[0];
    let done = false;
    const writer = s.write(new Uint8Array(4096 * 3).fill(120)).then(() => (done = true));
    await tick();
    expect(done).toBe(false); // window drained → write is parked
    let total = 0;
    while (total < 4096 * 3) total += (await rs.read(8192)).length;
    await writer;
    expect(done).toBe(true);
    expect(total).toBe(4096 * 3);
  });

  it("resets one stream without disturbing its sibling", async () => {
    const { a, opened } = link();
    const s1 = a.open(enc("keep"));
    const s2 = a.open(enc("kill"));
    await s1.write(enc("alive"));
    const by = new Map(opened.b.map((s) => [new TextDecoder().decode(s.openInfo), s]));
    s2.reset(9);
    await expect(by.get("kill")!.read()).rejects.toBeInstanceOf(StreamReset);
    expect(new TextDecoder().decode(await by.get("keep")!.read(1024))).toBe("alive");
  });
});
