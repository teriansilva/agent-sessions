/** Mint the new-session id for an engine (#163). opencode and codex can't pin their own
 *  ids (#127/#315): the ws `new=1` launch only accepts a `new-<uuid>` placeholder,
 *  which it reconciles to the engine's real id. Every other engine pins the
 *  client-minted UUID directly. Minting a bare UUID for a reconcile-engine makes the
 *  launch fail `parse_key` → reject(4404) "session not found". */
export function mintNewSessionId(engine: string): string {
  const uuid = crypto.randomUUID();
  return engine === "opencode" || engine === "codex" ? `new-${uuid}` : uuid;
}
