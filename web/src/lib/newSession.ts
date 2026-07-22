/** Engines that mint their OWN session id and launch under a `new-<uuid>` placeholder the ws
 *  `new=1` path reconciles to the real id (#127/#315/#449) — opencode, codex, antigravity
 *  (`agy`), and kimi (#714, whose `-S/--session` only resumes: there is no id-pinning flag).
 *  Must stay in lockstep with the providers' server-side `new_session_reconciles` flag:
 *  a reconcile-engine missing here mints a bare UUID and the launch fails `parse_key` →
 *  reject(4404) "session not found" (the #454 regression). Every other engine pins the
 *  client-minted UUID directly (claude, gemini). */
const RECONCILE_ENGINES = new Set(["opencode", "codex", "antigravity", "kimi"]);

/** Mint the new-session id for an engine (#163). */
export function mintNewSessionId(engine: string): string {
  const uuid = crypto.randomUUID();
  return RECONCILE_ENGINES.has(engine) ? `new-${uuid}` : uuid;
}
