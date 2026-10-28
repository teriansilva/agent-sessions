/** The sessions a conversation follows, as ONE live block (#1755): a row per session holding its
 *  latest update, never a row per change. Pure, and in its own module so `AskConsole.tsx`
 *  exports components only (Fast Refresh). */
import type { AskFollow, AskUpdate } from "../../types/api";

export const ASK_SEEN_EVENT = "agent-sessions:ask-seen";

export interface FollowRow {
  key: string;
  title: string;
  /** The newest update, or `null`: followed, no change yet. */
  latest: AskUpdate | null;
  /** How many updates this session has had since it was (last) followed. */
  changes: number;
  /** Where its newest update arrived in `updates` (-1: none): arrival order, never time alone. */
  order: number;
  /** Still followed: Unfollow is offered. */
  followed: boolean;
}

const isNotice = (u: AskUpdate) => u.kind === "ended" || u.kind === "not_followed";

/** One row per session: every active follow, and every session whose newest update is a notice
 *  (`ended`, `not_followed`). A session the operator unfollowed, whose newest update is no notice,
 *  is dropped: they chose to stop hearing about it. A session followed AGAIN keeps its old updates
 *  on the server, so each follow is a generation: `since` (the server's `follow_since`, kept
 *  after Unfollow) says when a session's latest follow began, and an update older than that is
 *  an earlier follow's — superseded, so a re-follow never inherits "No longer followed" and an
 *  Unfollow after it never brings that notice back. Without `since`, a notice before an ACTIVE
 *  follow still closes the generation. Needs-you rows come first, then the newest change;
 *  follows with no change yet keep their order, last. */
export function followRows(
  follows: AskFollow[],
  updates: AskUpdate[],
  since: Record<string, number> = {},
): FollowRow[] {
  const byKey = new Map<string, AskUpdate[]>();
  for (const u of updates) {
    if (u.ts < (since[u.key] ?? -Infinity)) continue;
    byKey.set(u.key, [...(byKey.get(u.key) ?? []), u]);
  }
  const titles = new Map(follows.map((f) => [f.key, f.title]));
  const rows: FollowRow[] = [];
  for (const key of new Set([...titles.keys(), ...byKey.keys()])) {
    let us = byKey.get(key) ?? [];
    if (titles.has(key)) {
      // Followed now: only this generation counts — the updates after the last notice.
      let cut = -1;
      us.forEach((u, i) => {
        if (isNotice(u)) cut = i;
      });
      us = us.slice(cut + 1);
    }
    const latest = us.reduce<AskUpdate | null>((m, u) => (!m || u.ts >= m.ts ? u : m), null);
    const order = latest ? updates.lastIndexOf(latest) : -1;
    const notice = latest ? isNotice(latest) : false;
    const followed = titles.has(key) || (!notice && Boolean(latest?.following));
    if (!followed && !notice) continue;
    const title = titles.get(key) || latest?.title || "";
    rows.push({ key, title, latest, changes: us.length, order, followed });
  }
  const rank = (r: FollowRow) => (r.latest?.kind === "needs_you" && r.followed ? 0 : 1);
  // `sort` is stable: rows with no change yet keep the follows' order.
  return rows.sort((a, b) => rank(a) - rank(b) || (b.latest?.ts ?? 0) - (a.latest?.ts ?? 0));
}
