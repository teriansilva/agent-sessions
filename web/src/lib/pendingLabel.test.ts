import { expect, test } from "vitest";
import { pendingLabel } from "./pendingLabel";

/** An Ask match says what is waiting in the session it found (#758 review).
 *
 *  All four `LIVE_STATES` reach this label and they do NOT all ask the operator for something.
 *  `claimed` in particular is set immediately BEFORE the actuator writes, so telling someone
 *  their approval is still needed there is false — and a flag that overstates what is required
 *  spends the credibility of every accurate one. */
const p = (state: string, verb = "continue") => ({
  action_id: "a1",
  state,
  verb,
});

test("an escalation asks for a decision", () => {
  expect(pendingLabel(p("escalated"))).toMatch(/needs a decision from you/i);
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
