/** Open a reply link's sealed envelope in the browser (#1956). The format is
 *  `src/agent_sessions/reply_share.py`'s, held together by `fixtures/envelopes.json` (sealed there,
 *  opened here in `seal.test.ts`).
 *
 *  The key `K` comes from the link's fragment and never leaves this page: nothing here sends it,
 *  and the page fetches only the ciphertext. With a password, `CK = HKDF(K ‖ PBKDF2(pw))`, so the
 *  password never leaves either. */

export interface ReplyEnvelope {
  v: 1;
  id: string;
  expires_at: number | null;
  password: boolean;
  salt: string | null;
  nonce: string;
  ct: string;
}

export interface ReplyContent {
  v: 1;
  agent: string;
  reply: string;
  ts: number | null;
  tools_hidden: number;
  tools_more: boolean;
}

export const PBKDF2_ITERATIONS = 600_000;
const INFO = new TextEncoder().encode("battlelab-reply-v1");

export class WrongKeyError extends Error {}

/** WebCrypto takes a buffer-backed view only (not a SharedArrayBuffer-backed one). */
type Bytes = Uint8Array<ArrayBuffer>;

export function unb64u(text: string): Bytes {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function aad(env: ReplyEnvelope): Bytes {
  return new TextEncoder().encode(`${env.id}|${env.expires_at ?? 0}`);
}

async function contentKey(k: Bytes, password: string | null, salt: Bytes | null) {
  let ikm: Bytes = k;
  if (password !== null && salt) {
    const pw = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(password),
      "PBKDF2",
      false,
      ["deriveBits"],
    );
    const p = new Uint8Array(
      await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
        pw,
        256,
      ),
    );
    ikm = new Uint8Array(new ArrayBuffer(k.length + p.length));
    ikm.set(k);
    ikm.set(p, k.length);
  }
  const base = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32), info: INFO },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );
}

/** The snapshot, or `WrongKeyError` when the key or password doesn't open it. */
export async function openEnvelope(
  env: ReplyEnvelope,
  key: string,
  password: string | null = null,
): Promise<ReplyContent> {
  let k: Bytes;
  try {
    k = unb64u(key);
  } catch {
    throw new WrongKeyError("this link's key is incomplete");
  }
  if (k.length !== 32) throw new WrongKeyError("this link's key is incomplete");
  const ck = await contentKey(
    k,
    env.password ? password ?? "" : null,
    env.salt ? unb64u(env.salt) : null,
  );
  let plain: ArrayBuffer;
  try {
    plain = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: unb64u(env.nonce), additionalData: aad(env) },
      ck,
      unb64u(env.ct),
    );
  } catch {
    throw new WrongKeyError(env.password ? "wrong password" : "this link's key doesn't match");
  }
  return JSON.parse(new TextDecoder().decode(plain)) as ReplyContent;
}

/** `{id, key}` from a reply link: `/r/<id>#k=<key>` on a box. */
export function parseLink(loc: { pathname: string; hash: string }): { id: string | null; key: string | null } {
  const m = /^\/r\/([A-Za-z0-9_-]{22})\/?$/.exec(loc.pathname);
  const params = new URLSearchParams(loc.hash.replace(/^#/, ""));
  return { id: m ? m[1] : null, key: params.get("k") };
}

/** Where the envelope for `id` lives on this origin. */
export function envelopeUrl(id: string): string {
  return `/api/public/reply/${encodeURIComponent(id)}`;
}
