import { mintsOwnId } from "../app/engineRoster";

/** Mint the new-session id for an engine (#163), or `null` while that is not yet KNOWABLE.
 *
 *  An engine that mints its OWN id (`session_id.mint = "adopt"` in its manifest — opencode, codex,
 *  antigravity, kimi today) launches under a `new-<uuid>` placeholder that the ws `new=1` path
 *  reconciles to the real id (#127/#315/#449); every other engine pins the client-minted UUID.
 *  The answer comes from the engine roster (#853 P4), never a client-side set: that set drifting
 *  from the server is the #454 regression (a bare UUID for a reconcile engine fails `parse_key`
 *  → 4404). Before the roster has loaded — or for an engine it does not list — the honest answer
 *  is "not yet", and the caller must WAIT rather than guess. */
export function mintNewSessionId(engine: string): string | null {
  const own = mintsOwnId(engine);
  if (own === undefined) return null;
  const uuid = crypto.randomUUID();
  return own ? `new-${uuid}` : uuid;
}
