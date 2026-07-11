// @vitest-environment node
//
// Cross-implementation test: the browser (WebCrypto) handshake must reproduce
// the Python reference bytes exactly. Runs in the node environment so it uses
// Node's WebCrypto (jsdom's SubtleCrypto is incomplete). The fixture is
// generated from Python by scripts/gen_homefree_vectors.py.

import { describe, expect, it } from "vitest";

import { Initiator, Responder, derivePsk } from "./handshake";
import vectors from "./handshake.vectors.json";

function hex(s: string): Uint8Array {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function toHex(u: Uint8Array): string {
  return [...u].map((b) => b.toString(16).padStart(2, "0")).join("");
}

describe("Home Free handshake — browser mirror vs Python vectors", () => {
  it("derives the same PSK from the access key", async () => {
    const psk = await derivePsk(vectors.access_key);
    expect(toHex(psk)).toBe(vectors.psk_hex);
  });

  it("initiator produces the pinned e_i (msg1)", () => {
    const ini = new Initiator(hex(vectors.psk_hex), hex(vectors.initiator_ephemeral_priv_hex));
    expect(toHex(ini.start())).toBe(vectors.e_i_hex);
  });

  it("responder produces the pinned msg2 and initiator accepts conf_r", async () => {
    const psk = hex(vectors.psk_hex);
    const res = new Responder(psk, hex(vectors.responder_ephemeral_priv_hex));
    const msg2 = await res.respond(hex(vectors.e_i_hex));
    expect(toHex(msg2)).toBe(vectors.msg2_hex);

    const ini = new Initiator(psk, hex(vectors.initiator_ephemeral_priv_hex));
    const { msg3 } = await ini.finish(msg2);
    expect(toHex(msg3)).toBe(vectors.msg3_hex); // initiator confirmation matches Python
  });

  it("reproduces the pinned transport frames (initiator -> responder)", async () => {
    const psk = hex(vectors.psk_hex);
    const ini = new Initiator(psk, hex(vectors.initiator_ephemeral_priv_hex));
    const res = new Responder(psk, hex(vectors.responder_ephemeral_priv_hex));
    const msg2 = await res.respond(ini.start());
    const { transport } = await ini.finish(msg2);
    for (const frame of vectors.transport.i2r) {
      const out = await transport.encrypt(hex(frame.plaintext_hex));
      expect(toHex(out)).toBe(frame.frame_hex); // byte-for-byte identical ciphertext
    }
  });

  it("decrypts the pinned transport frames (responder -> initiator)", async () => {
    const psk = hex(vectors.psk_hex);
    const ini = new Initiator(psk, hex(vectors.initiator_ephemeral_priv_hex));
    const res = new Responder(psk, hex(vectors.responder_ephemeral_priv_hex));
    const msg2 = await res.respond(ini.start());
    const { transport } = await ini.finish(msg2);
    for (const frame of vectors.transport.r2i) {
      const pt = await transport.decrypt(hex(frame.frame_hex));
      expect(toHex(pt)).toBe(frame.plaintext_hex);
    }
  });

  it("full self-consistent handshake round-trips (random ephemerals)", async () => {
    const psk = await derivePsk("some-other-access-key");
    const ini = new Initiator(psk);
    const res = new Responder(psk);
    const msg2 = await res.respond(ini.start());
    const { transport: tIni, msg3 } = await ini.finish(msg2);
    const tRes = await res.finish(msg3);

    const frame = await tIni.encrypt(new TextEncoder().encode("ping"));
    expect(new TextDecoder().decode(await tRes.decrypt(frame))).toBe("ping");
    const back = await tRes.encrypt(new TextEncoder().encode("pong"));
    expect(new TextDecoder().decode(await tIni.decrypt(back))).toBe("pong");
  });

  it("rejects a wrong PSK at the responder confirmation", async () => {
    const res = new Responder(await derivePsk("wrong"));
    const msg2 = await res.respond(new Initiator(await derivePsk("right")).start());
    const ini = new Initiator(await derivePsk("right"));
    await expect(ini.finish(msg2)).rejects.toThrow();
  });

  it("rejects a replayed transport frame", async () => {
    const psk = await derivePsk("k");
    const ini = new Initiator(psk);
    const res = new Responder(psk);
    const msg2 = await res.respond(ini.start());
    const { transport: tIni, msg3 } = await ini.finish(msg2);
    const tRes = await res.finish(msg3);

    const frame = await tIni.encrypt(new Uint8Array([1, 2, 3]));
    expect([...(await tRes.decrypt(frame))]).toEqual([1, 2, 3]);
    await expect(tRes.decrypt(frame)).rejects.toThrow(); // same counter → replay
  });
});
