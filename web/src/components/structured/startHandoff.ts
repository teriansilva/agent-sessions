/** A skip start's own outcome, handed from New session to the session pane (#1505).
 *
 *  New session opens the session page right after create and fires the skip `/start` without
 *  waiting for it. The request's outcome is never swallowed: it is kept here under the SAME
 *  session key, and the pane that shows that session reads it once.
 *
 *  - `ok`: the server answered at launch; its snapshot carries `launch: starting`.
 *  - `refused`: a definite answer (4xx) — the server also recorded it in the snapshot's launch
 *    view, so the pane shows the reason with Start again / Discard.
 *  - `unknown`: no answer, or an ambiguous one (network error, timeout, 5xx). The start may or
 *    may not have reached BattleLab; the pane says exactly that and offers Check again (re-read
 *    the snapshot) and Start again (the server's reconcile is idempotent).
 *
 *  In-memory and per tab: nothing here survives a reload, where the snapshot alone is the truth.
 */
import { ApiError } from "../../lib/api";
import type { StructuredSnapshot } from "../../types/api";

export type StartOutcome =
  | { kind: "ok"; snap: StructuredSnapshot }
  | { kind: "refused"; reason: string }
  | { kind: "unknown" };

/** Bounded: an outcome nobody opened (the operator went elsewhere) never piles up. */
const MAX_HELD = 16;
const held = new Map<string, Promise<StartOutcome>>();

function outcome(e: unknown): StartOutcome {
  if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
    return { kind: "refused", reason: e.message || "the start was refused" };
  }
  return { kind: "unknown" };
}

/** Hand the in-flight start request for `key` to whichever pane opens that session. */
export function handStart(key: string, request: Promise<StructuredSnapshot>): void {
  const settled = request.then(
    (snap): StartOutcome => ({ kind: "ok", snap }),
    (e: unknown) => outcome(e),
  );
  held.delete(key);
  held.set(key, settled);
  while (held.size > MAX_HELD) {
    const oldest = held.keys().next().value;
    if (oldest === undefined) break;
    held.delete(oldest);
  }
}

/** The handed start for `key`, if any, WITHOUT consuming it (an effect may run twice). */
export function peekStart(key: string): Promise<StartOutcome> | undefined {
  return held.get(key);
}

/** Consume the handed start once its outcome has been shown — only if it is still that one. */
export function consumeStart(key: string, which: Promise<StartOutcome>): void {
  if (held.get(key) === which) held.delete(key);
}

/** Tests only. */
export function resetStartHandoff(): void {
  held.clear();
}
