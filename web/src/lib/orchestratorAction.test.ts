import { describe, expect, it } from "vitest";
import type { OrchestratorAction } from "../types/api";
import {
  OPERATOR_PENDING,
  actionOutcome,
  escalationSuffix,
  isEscalation,
} from "./orchestratorAction";

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

describe("the two roads into an escalation (#877)", () => {
  it("recognises both kinds, and nothing else", () => {
    // The predicate exists so the eighth reader asks a NAME instead of repeating a string. On
    // the server, seven exact `== "escalated"` comparisons decided things — announcement,
    // counting, tone, ARIA, whether the reason shows — and each one a new state missed failed
    // silently and differently.
    expect(isEscalation("escalated")).toBe(true);
    expect(isEscalation("escalated_low_confidence")).toBe(true);
    expect(isEscalation("proposed")).toBe(false);
    expect(isEscalation("approved")).toBe(false);
    expect(isEscalation(undefined)).toBe(false);
  });

  it("shows the escalation reason for BOTH kinds", () => {
    // Keyed on the predicate, not on the state name: a low-confidence row that missed this
    // would drop the one line explaining WHY it is waiting on the operator.
    expect(
      escalationSuffix(
        action({
          state: "escalated_low_confidence",
          escalation_reason: "confidence",
        }),
      ),
    ).toBe("below threshold");
    expect(
      escalationSuffix(
        action({ state: "escalated", escalation_reason: "model" }),
      ),
    ).toBe("needs your call");
  });

  it("keeps both kinds in the operator-pending set", () => {
    // Mirrors the server's `OPERATOR_PENDING_STATES`, which decides whether a card carries a
    // `pending_action` at all. Missing the new state here hides the decision entirely.
    expect(OPERATOR_PENDING.has("escalated")).toBe(true);
    expect(OPERATOR_PENDING.has("escalated_low_confidence")).toBe(true);
    expect(OPERATOR_PENDING.has("claimed")).toBe(false);
  });
});
