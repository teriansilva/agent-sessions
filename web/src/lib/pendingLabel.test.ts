import { expect, test } from "vitest";
import { pendingLabel } from "./pendingLabel";

/** An Ask match says what is waiting in the session it found (#758 review).
 *
 *  Every `LIVE_STATES` member reaches this label — FIVE since #877 split the escalations into
 *  the model's question (`escalated`, dismiss-only) and a low-confidence action
 *  (`escalated_low_confidence`, approvable) — and they do NOT all ask the operator for
 *  something. `claimed` in particular is set immediately BEFORE the actuator writes, so telling
 *  someone their approval is still needed there is false — and a flag that overstates what is
 *  required spends the credibility of every accurate one. */
const p = (state: string, verb = "continue") => ({
  action_id: "a1",
  state,
  verb,
});

test("an escalation asks for a decision — BOTH kinds", () => {
  expect(pendingLabel(p("escalated"))).toMatch(/needs a decision from you/i);
  // The eighth exact `== "escalated"` comparison (#877). The issue's own inventory listed
  // seven, and this one fell through to the default: a low-confidence `continue` was reported
  // as "in flight" — delivery underway — while it was in fact waiting for the very Approve
  // that PR had just made possible. Overstating is the same failure as inventing an errand.
  expect(pendingLabel(p("escalated_low_confidence"))).toMatch(
    /needs a decision from you/i,
  );
});

test("an unknown live state still says something, without claiming a decision", () => {
  // The default has to stay reachable and stay vague: a bundle can meet a state from a newer
  // server. What it must never do is absorb a state that DOES need the operator.
  expect(pendingLabel(p("some-future-state"))).toMatch(/in flight/i);
  expect(pendingLabel(p("some-future-state"))).not.toMatch(/decision/i);
});

test("a proposal asks for approval", () => {
  expect(pendingLabel(p("proposed"))).toMatch(/waiting for your approval/i);
});

test("an approved action does not claim it still needs approving", () => {
  const out = pendingLabel(p("approved"));
  expect(out).toMatch(/approved and queued/i);
  expect(out).not.toMatch(/waiting for your approval/i);
});

test("a claimed action says delivery is under way, not that approval is needed", () => {
  const out = pendingLabel(p("claimed"));
  expect(out).toMatch(/being delivered now/i);
  expect(out).not.toMatch(/approval/i);
});

test("an unknown live state is surfaced without claiming what it needs", () => {
  const out = pendingLabel(p("something-new"));
  expect(out).toMatch(/in flight/i);
  expect(out).not.toMatch(/approval|decision/i);
});

test("a missing verb still reads as a sentence", () => {
  expect(pendingLabel({ action_id: "a", state: "proposed", verb: "" })).toMatch(
    /an action is waiting/i,
  );
});
