// Browser mirror of the Home Free E2E handshake — interoperates byte-for-byte
// with the Python reference (src/agent_sessions/homefree/handshake.py). The wire
// format is pinned in docs/home-free-handshake.md; the shared vector fixture
// (handshake.vectors.json) guards both implementations against drift.
//
// Primitives: X25519 via @noble/curves (universal browser support), HKDF-SHA256
// and AES-256-GCM via WebCrypto. The relay never sees any of this.

import { x25519 } from "@noble/curves/ed25519.js";

const enc = new TextEncoder();
const PROTO = enc.encode("battlelab-home-free/v1");
const PSK_INFO = enc.encode("battlelab-home-free psk v1");
const KEYS_INFO = enc.encode("bl-hf keys v1");
const ZERO_NONCE = new Uint8Array(12);

export class HandshakeError extends Error {}

function subtle(): SubtleCrypto {
  return globalThis.crypto.subtle;
}

// TS 5.7+ types Uint8Array as Uint8Array<ArrayBufferLike>, which WebCrypto's
// BufferSource (ArrayBuffer-backed) does not accept. Our buffers are never
// SharedArrayBuffer, so this coercion at the SubtleCrypto boundary is safe.
function src(u: Uint8Array): BufferSource {
  return u as unknown as BufferSource;
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

function u64be(value: bigint): Uint8Array {
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, value, false);
  return out;
}

function nonceFor(counter: bigint): Uint8Array {
  const n = new Uint8Array(12);
  new DataView(n.buffer).setBigUint64(4, counter, false); // 4 zero bytes || BE counter
  return n;
}

async function sha256(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await subtle().digest("SHA-256", src(data)));
}

async function hkdf(
  ikm: Uint8Array,
  salt: Uint8Array,
  info: Uint8Array,
  lengthBytes: number,
): Promise<Uint8Array> {
  const key = await subtle().importKey("raw", src(ikm), "HKDF", false, ["deriveBits"]);
  const bits = await subtle().deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: src(salt), info: src(info) },
    key,
    lengthBytes * 8,
  );
  return new Uint8Array(bits);
}

async function importAes(key: Uint8Array): Promise<CryptoKey> {
  return subtle().importKey("raw", src(key), "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function aesEncrypt(
  key: CryptoKey,
  nonce: Uint8Array,
  plaintext: Uint8Array,
  aad?: Uint8Array,
): Promise<Uint8Array> {
  const params: AesGcmParams = { name: "AES-GCM", iv: src(nonce), tagLength: 128 };
  if (aad) params.additionalData = src(aad);
  return new Uint8Array(await subtle().encrypt(params, key, src(plaintext)));
}

async function aesDecrypt(
  key: CryptoKey,
  nonce: Uint8Array,
  ciphertext: Uint8Array,
  aad?: Uint8Array,
): Promise<Uint8Array> {
  const params: AesGcmParams = { name: "AES-GCM", iv: src(nonce), tagLength: 128 };
  if (aad) params.additionalData = src(aad);
  return new Uint8Array(await subtle().decrypt(params, key, src(ciphertext)));
}

export async function derivePsk(accessKey: string): Promise<Uint8Array> {
  return hkdf(enc.encode(accessKey), new Uint8Array(0), PSK_INFO, 32);
}

interface SessionKeys {
  kI2r: Uint8Array;
  kR2i: Uint8Array;
  th: Uint8Array;
}

async function deriveSessionKeys(
  dh: Uint8Array,
  psk: Uint8Array,
  eI: Uint8Array,
  eR: Uint8Array,
): Promise<SessionKeys> {
  const th = await sha256(concat(PROTO, eI, eR));
  const okm = await hkdf(dh, psk, concat(KEYS_INFO, th), 64);
  return { kI2r: okm.slice(0, 32), kR2i: okm.slice(32, 64), th };
}

/** A bidirectional AES-256-GCM stream with per-direction monotonic counters. */
export class Transport {
  private sendCtr = 0n;
  private recvSeen = 0n;
  private readonly sendKey: CryptoKey;
  private readonly recvKey: CryptoKey;

  private constructor(sendKey: CryptoKey, recvKey: CryptoKey) {
    this.sendKey = sendKey;
    this.recvKey = recvKey;
  }

  static async create(sendKey: Uint8Array, recvKey: Uint8Array): Promise<Transport> {
    return new Transport(await importAes(sendKey), await importAes(recvKey));
  }

  async encrypt(plaintext: Uint8Array): Promise<Uint8Array> {
    this.sendCtr += 1n;
    const ctr = this.sendCtr;
    const blob = await aesEncrypt(this.sendKey, nonceFor(ctr), plaintext);
    return concat(u64be(ctr), blob);
  }

  async decrypt(frame: Uint8Array): Promise<Uint8Array> {
    if (frame.length < 8) throw new HandshakeError("short transport frame");
    const ctr = new DataView(frame.buffer, frame.byteOffset, 8).getBigUint64(0, false);
    if (ctr <= this.recvSeen) throw new HandshakeError("replayed or out-of-order frame");
    const plaintext = await aesDecrypt(this.recvKey, nonceFor(ctr), frame.subarray(8));
    this.recvSeen = ctr;
    return plaintext;
  }
}

function newEphemeral(injected?: Uint8Array): Uint8Array {
  // Production: a fresh random ephemeral each handshake (forward secrecy).
  // ``injected`` is a TEST-ONLY hook for deterministic vectors.
  return injected ?? globalThis.crypto.getRandomValues(new Uint8Array(32));
}

/** Browser side of the handshake (initiator). */
export class Initiator {
  private readonly psk: Uint8Array;
  private readonly priv: Uint8Array;
  private readonly eI: Uint8Array;

  constructor(psk: Uint8Array, ephemeralPriv?: Uint8Array) {
    this.psk = psk;
    this.priv = newEphemeral(ephemeralPriv);
    this.eI = x25519.getPublicKey(this.priv);
  }

  start(): Uint8Array {
    return this.eI;
  }

  async finish(msg2: Uint8Array): Promise<{ transport: Transport; msg3: Uint8Array }> {
    if (msg2.length !== 48) throw new HandshakeError("bad msg2 length");
    const eR = msg2.subarray(0, 32);
    const confR = msg2.subarray(32);
    const dh = x25519.getSharedSecret(this.priv, eR);
    const { kI2r, kR2i, th } = await deriveSessionKeys(dh, this.psk, this.eI, eR);
    try {
      await aesDecrypt(await importAes(kR2i), ZERO_NONCE, confR, th);
    } catch {
      throw new HandshakeError("responder confirmation failed (bad PSK?)");
    }
    const confI = await aesEncrypt(await importAes(kI2r), ZERO_NONCE, new Uint8Array(0), th);
    return { transport: await Transport.create(kI2r, kR2i), msg3: confI };
  }
}

/** Agent side of the handshake (responder) — provided for cross-impl testing. */
export class Responder {
  private readonly psk: Uint8Array;
  private readonly priv: Uint8Array;
  private readonly eR: Uint8Array;
  private keys?: SessionKeys;

  constructor(psk: Uint8Array, ephemeralPriv?: Uint8Array) {
    this.psk = psk;
    this.priv = newEphemeral(ephemeralPriv);
    this.eR = x25519.getPublicKey(this.priv);
  }

  async respond(msg1: Uint8Array): Promise<Uint8Array> {
    if (msg1.length !== 32) throw new HandshakeError("bad msg1 length");
    const dh = x25519.getSharedSecret(this.priv, msg1);
    this.keys = await deriveSessionKeys(dh, this.psk, msg1, this.eR);
    const confR = await aesEncrypt(
      await importAes(this.keys.kR2i),
      ZERO_NONCE,
      new Uint8Array(0),
      this.keys.th,
    );
    return concat(this.eR, confR);
  }

  async finish(msg3: Uint8Array): Promise<Transport> {
    if (!this.keys) throw new HandshakeError("finish() before respond()");
    if (msg3.length !== 16) throw new HandshakeError("bad msg3 length");
    try {
      await aesDecrypt(await importAes(this.keys.kI2r), ZERO_NONCE, msg3, this.keys.th);
    } catch {
      throw new HandshakeError("initiator confirmation failed (bad PSK?)");
    }
    return Transport.create(this.keys.kR2i, this.keys.kI2r);
  }
}
