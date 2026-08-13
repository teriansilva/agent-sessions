import { describe, expect, it } from "vitest";
import type { OrchestratorAction } from "../types/api";
import { actionOutcome, escalationSuffix } from "./orchestratorAction";

function action(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "a1",
    state: "escalated",
    ts: 0,
    tier: "suggest",
    session_id: "claude:11111111-1111-4111-8111-111111111111",
    engine: "claude",
    title: "t",
    project: "p",
    project_id: "pid",
    verb: "escalate",
    confidence: 0.9,
    rationale: "",
    evidence: "none",
    ...over,
  } as OrchestratorAction;
}

describe("escalationSuffix", () => {
  it("names the cause the SERVER recorded, not the state", () => {
    // The bug: every escalated row said "below threshold". Only one of the three paths into
    // `escalated` is the threshold, and it is unreachable at any tier but `yolo`.
    expect(escalationSuffix(action({ escalation_reason: "model" }))).toBe(
      "needs your call",
    );
    expect(escalationSuffix(action({ escalation_reason: "degraded" }))).toBe(
      "nothing deliverable",
    );
    expect(escalationSuffix(action({ escalation_reason: "confidence" }))).toBe(
      "below threshold",
    );
  });

  it("says nothing for a record written before the reason existed", () => {
    // Not a gap: the server cannot know why after the fact, and an absent explanation beats
    // a possibly-false one. This is what every escalation already in the ledger renders as.
    expect(escalationSuffix(action())).toBeUndefined();
  });

  it("says nothing for a reason it does not recognise", () => {
    // The server owns this vocabulary. A client built against an older copy must degrade to
    // silence rather than invent copy — and must not leave a dangling " · " behind.
    expect(
      escalationSuffix(
        action({
          escalation_reason: "some-future-reason" as never,
        }),
      ),
    ).toBeUndefined();
  });

  it("says nothing on an action that did not escalate", () => {
    // A `proposed` row is about a decision you can still make, not one already handed back.
    expect(
      escalationSuffix(
        action({
          state: "proposed",
          verb: "continue",
          escalation_reason: "confidence",
        }),
      ),
    ).toBeUndefined();
  });
});

describe("actionOutcome", () => {
  it("says what became of a settled action, not which state it reached", () => {
    expect(actionOutcome("expired")).toBe("no decision in time");
    expect(actionOutcome("rejected")).toBe("dismissed");
    expect(actionOutcome("observed")).toBe("left alone");
    expect(actionOutcome("delivered")).toBe("sent");
    expect(actionOutcome("stale")).toBe("session moved on");
    expect(actionOutcome("indeterminate")).toBe("outcome unknown");
    expect(actionOutcome("failed")).toBe("failed");
  });

  it("falls back to the raw name for a state it does not know", () => {
    // The type makes the map exhaustive at build time, but the state arrives at RUNTIME from a
    // server that may be newer than this bundle. Degrading to the raw name keeps the line
    // truthful; rendering blank would silently drop the outcome.
    expect(actionOutcome("some-future-state")).toBe("some-future-state");
  });
});
