/** An AI-drafted direction's decision card, decided without a DOM (#983 P3). Fixtures are the shape
 *  `mission_supervisor.propose_draft` mints and `routes/pulse._operator_projection` projects. */
import { describe, expect, test } from "vitest";

import type { OrchestratorAction } from "../../types/api";

import { DRAFT_VERB, draftView, isDraftDirection } from "./draftDirection";

const TEXT = "Add a regression test for the backoff.\n  Run it,  then push.  ";

function draft(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "act_draft",
    state: "proposed",
    ts: 1,
    tier: "suggest",
    session_id: "claude:aaa",
    engine: "claude",
    title: "An AI-drafted direction is waiting for your tap",
    project: "",
    project_id: "",
    verb: "draft_direction",
    confidence: 0,
    rationale: "",
    evidence: "none",
    source: "supervisor",
    mission_id: "msn_1",
    objective_key: "review",
    objective_title: "A reviewer approved the PR",
    draft: TEXT,
    can_approve: true,
    can_reject: true,
    ...over,
  };
}

const ALL = { approvable: true, rejectable: true, editable: true };

describe("what counts as a draft", () => {
  test("the server's verb, from the supervisor, carrying its text", () => {
    expect(DRAFT_VERB).toBe("draft_direction");
    expect(isDraftDirection(draft())).toBe(true);
    expect(isDraftDirection(draft({ source: "orchestrator" }))).toBe(false);
    expect(isDraftDirection(draft({ verb: "answer" }))).toBe(false);
    expect(isDraftDirection(draft({ draft: undefined }))).toBe(false);
    expect(draftView(draft({ verb: "continue" }), ALL)).toBeNull();
  });
});

describe("the card", () => {
  test("shows the stored draft verbatim and names its objective", () => {
    const v = draftView(draft(), ALL)!;
    expect(v.text).toBe(TEXT);
    expect(v.objective).toBe("A reviewer approved the PR");
    expect(draftView(draft({ objective_title: "  " }), ALL)!.objective).toBe("review");
  });

  test("Send as written is exactly the server's Approve", () => {
    expect(draftView(draft(), { ...ALL, approvable: false })!.canSend).toBe(false);
    expect(draftView(draft(), ALL)!.canSend).toBe(true);
  });

  test("Edit needs a composer AND a draft that can still be replaced", () => {
    expect(draftView(draft(), { ...ALL, editable: false })!.canEdit).toBe(false);
    expect(draftView(draft(), { ...ALL, rejectable: false })!.canEdit).toBe(false);
    expect(draftView(draft(), ALL)!.canEdit).toBe(true);
  });

  test("Dismiss follows the server's Reject", () => {
    expect(draftView(draft(), { ...ALL, rejectable: false })!.canDismiss).toBe(false);
    expect(draftView(draft(), ALL)!.canDismiss).toBe(true);
  });
});

describe("the opt-in decides the card's promise (#983 P4)", () => {
  test("with the mode off — the default — the card makes P3's promise", () => {
    // Absent and explicitly-off must agree: a surface that does not know reads as off.
    expect(draftView(draft(), ALL)!.autoThreshold).toBeNull();
    expect(
      draftView(draft(), { ...ALL, auto: { on: false, threshold: 0.9 } })!.autoThreshold,
    ).toBeNull();
    expect(draftView(draft(), { ...ALL, auto: null })!.autoThreshold).toBeNull();
  });

  test("with it on, the card carries the operator's own threshold", () => {
    expect(
      draftView(draft(), { ...ALL, auto: { on: true, threshold: 0.95 } })!.autoThreshold,
    ).toBe(0.95);
  });

  test("a threshold that is not a finite number is not a promise", () => {
    // `NaN.toFixed(2)` renders "NaN", which would read as a real setting on the card.
    expect(
      draftView(draft(), { ...ALL, auto: { on: true, threshold: Number.NaN } })!.autoThreshold,
    ).toBeNull();
  });
});
