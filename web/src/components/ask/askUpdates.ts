/** Where a followed session's update sits in the thread (#1443 Phase 4): between the turns asked
 *  before and after it, by time. Pure, and in its own module so `AskConsole.tsx` exports
 *  components only (Fast Refresh). */
import type { AskUpdate } from "../../types/api";

export const ASK_SEEN_EVENT = "agent-sessions:ask-seen";

/** `before`: updates older than the first turn. `after`: each turn's updates, newer than it and
 *  older than the next turn. A turn with no time (one still being sent) sorts last. */
export function placeUpdates(
  turns: { id: string; at: number | null }[],
  updates: AskUpdate[],
): { before: AskUpdate[]; after: Map<string, AskUpdate[]> } {
  const after = new Map<string, AskUpdate[]>();
  const before: AskUpdate[] = [];
  const timed = turns.filter((t) => t.at !== null) as { id: string; at: number }[];
  for (const u of [...updates].sort((x, y) => x.ts - y.ts)) {
    let owner: string | null = null;
    for (const t of timed) {
      if (t.at <= u.ts) owner = t.id;
      else break;
    }
    if (owner === null) before.push(u);
    else after.set(owner, [...(after.get(owner) ?? []), u]);
  }
  return { before, after };
}
