/** Mint the new-session id for an engine (#163). opencode can't pin its own id (#127): the ws
 *  `new=1` launch only accepts a `new-<uuid>` placeholder, which it reconciles to opencode's
 *  real `ses_…`. Every other engine pins the client-minted UUID directly. Minting a bare UUID
 *  for opencode made the launch fail `parse_key` → reject(4404) "session not found". */
export function mintNewSessionId(engine: string): string {
  const uuid = crypto.randomUUID();
  return engine === "opencode" ? `new-${uuid}` : uuid;
}
