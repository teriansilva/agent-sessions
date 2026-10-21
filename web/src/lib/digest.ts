/** A stable content digest, for "the thing I approved is the thing you will use" (#893).
 *
 * The client cannot be trusted to CHOOSE anything the server acts on — a path, an action, a
 * session key — and this is not that. It is a COMPARAND: the server computes the same digest
 * over its own rows and refuses when they differ, so the only thing the client can do with a
 * wrong value is have its request rejected.
 *
 * `crypto.subtle` is available on every browser this app supports and over HTTPS, which is the
 * only way it is served (the trust model in `README.md` requires TLS). It is async, which is why
 * every caller awaits: a synchronous re-implementation of SHA-256 to avoid one `await` would be
 * a second thing to keep in step with the server's `hashlib`.
 */
export async function sha256Hex(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const buf = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

/** The encoding `missions.objectives_digest` hashes — the SERVER's rule, spelled once here.
 *
 * **Length-prefixed, because delimiters are forgeable** (#904 review 10, finding 3). The first
 * version joined fields with U+001F and rows with U+001E while a TITLE may contain either, so a
 * one-row checklist whose title embedded them serialized identically to a different two-row one —
 * and the second DISPATCH tap could then pass the server's compare-and-set for a checklist the
 * first tap never showed. A separator can always be spelled by the data it separates; a length
 * cannot.
 *
 * Lengths are **UTF-8 byte counts**: `String.length` is UTF-16 code units and Python's `len` is
 * code points, and they differ outside the BMP. `TextEncoder` is the measure both agree on.
 *
 * Sorted by KEY, which is an `[a-z0-9_-]` slug — the one thing that orders identically under
 * JavaScript's UTF-16 comparison and Python's code-point one. Sorting the encoded rows would put
 * a title's astral characters into the comparison and the two would disagree.
 */
export function objectivesDigestInput(
  rows: { key: string; title?: string | null; gate?: boolean }[],
): string {
  const bytes = (text: string) => new TextEncoder().encode(text).length;
  const field = (text: string) => `${bytes(text)}:${text}`;
  return [...rows]
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
    .map((o) => field(o.key) + field(o.title ?? "") + (o.gate ? "1" : "0"))
    .join("");
}
