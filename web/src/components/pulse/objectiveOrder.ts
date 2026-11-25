/** The objective list's pure rules (#967 P3), kept out of the component so they can be tested
 *  without React or the drag library.
 *
 *  - `moveKey` is the ONE computation of a new order. A drop, a keyboard move and the menu's
 *    Move up / Move down all go through it, and the route replaces the whole order
 *    (`missions._op_reorder` requires the full key list), so the result is posted as one
 *    `reorder` op carrying every key.
 *  - `inOrder` lays the server's rows out in an order the operator chose but the server has not
 *    answered yet — and refuses to when that order no longer names exactly those rows.
 *  - `sharedReason` decides when the supervisor's refusal sentence is said once for the section
 *    rather than on every row.
 */
import type { MissionSupervisor } from "../../types/api";

/** `keys` with the key at `from` moved to `to` (the others shift to make room). An out-of-range or
 *  no-op move returns an unchanged copy, never a list with a hole or a duplicate. */
export function moveKey(
  keys: readonly string[],
  from: number,
  to: number,
): string[] {
  const out = [...keys];
  if (
    from === to ||
    from < 0 ||
    to < 0 ||
    from >= out.length ||
    to >= out.length
  ) {
    return out;
  }
  const [key] = out.splice(from, 1);
  out.splice(to, 0, key);
  return out;
}

/** `rows` in the order `keys` names, or `rows` as they are when `keys` is absent or does not name
 *  exactly those rows once each. A pending order is a claim about THIS list; if an add, a drop or
 *  a poll changed the list underneath it, the server's order is the only honest one to show. */
export function inOrder<T extends { key: string }>(
  rows: readonly T[],
  keys: readonly string[] | null,
): T[] {
  if (!keys || keys.length !== rows.length) return [...rows];
  if (new Set(keys).size !== keys.length) return [...rows];
  const byKey = new Map(rows.map((r) => [r.key, r]));
  const out: T[] = [];
  for (const k of keys) {
    const row = byKey.get(k);
    if (!row) return [...rows];
    out.push(row);
  }
  return out;
}

/** The refusal sentence to show ONCE above the rows, or null when each row keeps its own.
 *
 *  Chosen from the STRUCTURED fields, never by reading the prose: `no_session` and
 *  `sessions_unreadable` are the two mission-level refusals, and `mission_supervisor.may_nudge`
 *  checks them before anything about an objective, so under either flag every row carries the
 *  same sentence. The sentence itself stays the server's, verbatim. It is the most common
 *  non-empty `why_not` (first seen wins a tie), so a row whose reason differs still shows its own.
 *
 *  Without either flag the reasons are about objectives — a spent budget, a stand-down, a question
 *  — and lifting one off its row would separate the sentence from the objective it explains. */
export function sharedReason(
  supervisor: MissionSupervisor | undefined,
): string | null {
  if (!supervisor) return null;
  if (!supervisor.no_session && !supervisor.sessions_unreadable) return null;
  const counts = new Map<string, number>();
  for (const o of supervisor.objectives ?? []) {
    if (o.why_not) counts.set(o.why_not, (counts.get(o.why_not) ?? 0) + 1);
  }
  let best: string | null = null;
  let most = 0;
  for (const [sentence, n] of counts) {
    if (n > most) {
      best = sentence;
      most = n;
    }
  }
  return best;
}

/** Does a clamped box hide some of its content? A pixel of slack, because a fractional line height
 *  rounds `scrollHeight` and `clientHeight` differently, and a title that fits must not grow a
 *  control over a rounding difference. A box with no height (a closed disclosure) hides nothing yet;
 *  it is measured again when it opens. */
export function clampOverflows(scrollHeight: number, clientHeight: number): boolean {
  if (clientHeight <= 0) return false;
  return scrollHeight > clientHeight + 1;
}
