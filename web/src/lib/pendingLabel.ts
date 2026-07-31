import type { PulseAskMatch } from "../types/api";

/** What a live orchestrator action means for the operator, per state.
 *
 *  All four `LIVE_STATES` reach here, and they do NOT all want the operator to do something —
 *  saying "waiting for your approval" for `claimed` is simply false, because the actuator moves
 *  an action to `claimed` immediately BEFORE writing to the session, so delivery is already
 *  under way. A flag that overstates what is required is the same failure as one that invents
 *  an errand: it sends the operator somewhere they are not needed and spends the credibility of
 *  every accurate flag. */
export function pendingLabel(p: NonNullable<PulseAskMatch["pending"]>): string {
  // "A action" reads as a bug to anyone who sees it, and an empty verb is a real shape — the
  // ledger only guarantees the field exists, not that it is populated.
  const subject = p.verb ? `A ${p.verb}` : "An action";
  switch (p.state) {
    case "escalated":
      return "Needs a decision from you";
    case "proposed":
      return `${subject} is waiting for your approval`;
    case "approved":
      return `${subject} is approved and queued`;
    case "claimed":
      return `${subject} is being delivered now`;
    default:
      // An unknown live state is still worth surfacing, but must not claim what it needs.
      return `${subject} is in flight`;
  }
}
