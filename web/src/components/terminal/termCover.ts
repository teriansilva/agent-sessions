import type { LookupPhase } from "../../app/sessionsStore";
import {
  CLOSE_NOT_FOUND,
  CLOSE_SESSION_LIMIT,
  CLOSE_STALE_DEFAULT,
} from "../../lib/termCloseCodes";
import type { TermStatus } from "../../lib/termSocket";

/** What covers the terminal area instead of its status chip (#1502), from the socket status and
 *  the row lookup for this exact key:
 *
 *  - `wrong-runtime` (4415): the page drew a terminal for an engine that is not one (a stale
 *    cached runtime). "Opening…" while the roster reloads NOW; the gate then draws the right pane.
 *  - 4404 while the lookup is still `pending`: a native-id link may be about to resolve to its
 *    owner, so it is not a verdict yet — "Opening…". Likewise once it HAS resolved to an owner
 *    (`aliased`): the route is about to replace the URL, and the frame in between must not read
 *    "not found" either.
 *  - 4404 once the lookup settled `missing`: the real "This session was not found".
 *  - 4404 with the lookup `failed` (a transient error, re-asked): never "not found" — an honest
 *    "couldn't check" in the chip.
 *  - 4412 (#1728): the launch's automatic default moved; nothing started — `stale-default`, which
 *    hands the refusal back to the New session form.
 *  - anything else: the chip as before (a 4404 for a session whose row exists still reads
 *    "session not found or ended": its master is gone). */
export type TermCover =
  | "opening"
  | "reopening"
  | "missing"
  | "stale-default"
  | "session-limit"
  | null;
export function termCover(
  status: TermStatus,
  lookup: LookupPhase | null,
  aliased = false,
): TermCover {
  if (status.kind === "wrong-runtime") return "reopening";
  // 4412 (#1728): a fresh launch's automatic default moved — nothing started; back to the form.
  if (status.kind === "rejected" && status.code === CLOSE_STALE_DEFAULT) return "stale-default";
  // 4429 (#1819): the session limit is reached — nothing started; free a slot and try again.
  if (status.kind === "rejected" && status.code === CLOSE_SESSION_LIMIT) return "session-limit";
  if (status.kind !== "rejected" || status.code !== CLOSE_NOT_FOUND) return null;
  if (lookup === "pending" || (lookup === "found" && aliased)) return "opening";
  if (lookup === "missing") return "missing";
  return null;
}

/** The chip text for a 4404 whose lookup could not be asked (#1502) — not "not found". */
export const LOOKUP_FAILED_TEXT = "couldn’t check this session — retrying";
