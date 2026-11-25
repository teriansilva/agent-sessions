/** The identity a compose draft is stored under (#477): the DURABLE session key, never a
 *  `new-…` placeholder.
 *
 *  The terminal's own `engine:id` stays frozen on the placeholder for the life of a converged
 *  session (#127/#315) — the socket depends on it — but a draft keyed on that would be disabled
 *  forever after the converge, and "Save as template" would keep insisting an already-started
 *  session had no id (Hermes on #908, round 5). `rowKey` is the id the URL has settled on; when
 *  it names a real session, that is the draft's home. Anything malformed falls back to the
 *  frozen identity, and a placeholder of either shape yields `null` — no draft storage yet. */
export function draftSessionKey(engine: string, id: string, rowKey?: string | null): string | null {
  let e = engine;
  let i = id;
  if (rowKey) {
    const at = rowKey.indexOf(":");
    const re = at > 0 ? rowKey.slice(0, at) : "";
    const ri = at > 0 ? rowKey.slice(at + 1) : "";
    if (re && ri) {
      e = re;
      i = ri;
    }
  }
  if (!e || !i || i.startsWith("new-")) return null;
  return `${e}:${i}`;
}
