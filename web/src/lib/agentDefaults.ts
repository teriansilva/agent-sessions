import { engineInfo, engineLabel } from "../app/engineRoster";

/** The one sentence every picker shows when the operator's stored default agent is not the one in
 *  effect (#1128). It names both engines, because a default that silently became another engine is
 *  the failure this exists to prevent — and it says the choice is kept, because it is: the stored
 *  value is never rewritten, and the default comes back on its own when the agent does. */
export function unavailableDefaultNotice(
  stored: string,
  fallback: string | null,
  action: "new" | "handoff" = "new",
): string {
  const what = action === "handoff" ? "handoffs" : "new sessions";
  const why = engineInfo(stored)?.present
    ? `is not available for ${what}`
    : "is not installed";
  const meanwhile = fallback
    ? `${what} use ${engineLabel(fallback)} until it is`
    : action === "handoff"
      ? "no installed agent can take a handoff until it is"
      : "no installed agent can start a new session until it is";
  return `Your default, ${engineLabel(stored)}, ${why} — ${meanwhile}. Your choice is kept.`;
}
