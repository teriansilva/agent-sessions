// Per-browser fingerprint + per-tab id (#184). The fingerprint is a stable random
// identifier persisted to localStorage so the server can recognise the same
// browser across reloads and reattaches. The tab id is in-memory only and
// distinguishes tabs/windows within one browser session.
//
// This is NOT auth or device fingerprinting — it's purely "same Chrome tab on
// the same machine" identity for the slice-3 ownership protocol. The server
// uses (fp, tab_id) as the claim key; a force takeover demotes the prior tuple.

const FP_STORAGE_KEY = "tr-browser-fp";
const FP_BYTES = 16; // 128 bits

let cachedFp: string | null = null;
let cachedTabId: string | null = null;

function randomHex(bytes: number): string {
  // Modern browsers expose crypto.getRandomValues; jsdom has it under crypto too.
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  let out = "";
  for (let i = 0; i < buf.length; i++) out += buf[i].toString(16).padStart(2, "0");
  return out;
}

/** Returns the persistent per-browser fingerprint, minting it on first call.
 *  Identity stable across reloads, restarts, and tabs of the same browser
 *  profile; a private window / different browser / cleared localStorage mints
 *  a fresh one. localStorage failure (Safari private mode quirks) falls back
 *  to an in-memory id for this session. */
export function getBrowserFp(): string {
  if (cachedFp) return cachedFp;
  try {
    const existing = localStorage.getItem(FP_STORAGE_KEY);
    if (existing && /^[0-9a-f]{32}$/.test(existing)) {
      cachedFp = existing;
      return existing;
    }
    const fresh = randomHex(FP_BYTES);
    localStorage.setItem(FP_STORAGE_KEY, fresh);
    cachedFp = fresh;
    return fresh;
  } catch {
    // Best-effort: an in-memory id is still enough for the lifetime of this tab.
    cachedFp = cachedFp ?? randomHex(FP_BYTES);
    return cachedFp;
  }
}

/** Returns a per-tab id — stable for the lifetime of this JS realm only.
 *  A page reload starts a fresh tab id (intentional: the previous owner's
 *  claim should expire via the lease grace and the new one takes over). */
export function getTabId(): string {
  if (cachedTabId) return cachedTabId;
  cachedTabId = randomHex(8); // 64 bits is plenty for a per-browser collision space
  return cachedTabId;
}

/** Test hook: forget the cached values so a unit test can simulate a fresh tab. */
export function _resetBrowserFpForTests(): void {
  cachedFp = null;
  cachedTabId = null;
}
