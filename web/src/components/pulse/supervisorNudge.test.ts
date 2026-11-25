/** The decision row for a supervisor nudge: when it may be sent, and what it says (#983 P2). */
import { describe, expect, test } from "vitest";

import type { OrchestratorAction } from "../../types/api";

import { isSupervisorNudge, nudgeView, provenance, sentence } from "./supervisorNudge";

const TEXT = "PR #412's checks are failing on fix/upload-retry.\n  Open the failing check.  ";

function nudge(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "a1",
    state: "proposed",
    ts: 1,
    tier: "suggest",
    session_id: "claude:s",
    engine: "claude",
    title: "The last push broke the lint step.",
    project: "",
    project_id: "",
    verb: "continue",
    confidence: 1,
    rationale: "",
    evidence: "none",
    source: "supervisor",
    mission_id: "msn_1",
    objective_key: "checks",
    objective_title: "Checks are green on the PR",
    render: {
      text: TEXT,
      source: "direction",
      facts: [
        { name: "pr", value: 412, observed_at: 60, target: { repo: "acme/app", head: "4b7e0d9c1" } },
        { name: "checks", value: "failure", observed_at: 120, target: { repo: "acme/app", head: "4b7e0d9c1" } },
      ],
    },
    render_status: { sendable: true, reason: "" },
    projection: "actionable",
    can_approve: true,
    can_reject: true,
    ...over,
  } as OrchestratorAction;
}

const BOTH = { approvable: true, rejectable: true };

describe("which actions are supervisor nudges", () => {
  test("a supervisor continue carrying its render", () => {
    expect(isSupervisorNudge(nudge())).toBe(true);
  });

  test("not an ordinary continue, another verb, or a nudge written before renders existed", () => {
    expect(isSupervisorNudge(nudge({ source: undefined }))).toBe(false);
    expect(isSupervisorNudge(nudge({ verb: "answer" }))).toBe(false);
    expect(isSupervisorNudge(nudge({ render: undefined }))).toBe(false);
    expect(nudgeView(nudge({ render: undefined }), BOTH)).toBeNull();
  });
});

describe("sendable", () => {
  test("a sendable nudge the server lets you approve offers Send and Reject", () => {
    const v = nudgeView(nudge(), BOTH)!;
    expect(v.sendable).toBe(true);
    expect(v.canSend).toBe(true);
    expect(v.canDismiss).toBe(true);
    expect(v.reason).toBe("");
  });

  test("render_status sendable=false refuses Send even where Approve would be offered", () => {
    const v = nudgeView(
      nudge({ render_status: { sendable: false, reason: "the PR head moved" } }),
      BOTH,
    )!;
    expect(v.sendable).toBe(false);
    expect(v.canSend).toBe(false);
    expect(v.canDismiss).toBe(true);
    expect(v.reason).toBe("the PR head moved");
  });

  test("the server withdrawing can_approve refuses Send even if render_status is missing", () => {
    const v = nudgeView(nudge({ render_status: undefined }), { approvable: false, rejectable: true })!;
    expect(v.sendable).toBe(true);
    expect(v.canSend).toBe(false);
  });

  test("not sendable without a reason still says something, and never the AI's why", () => {
    const v = nudgeView(nudge({ render_status: { sendable: false, reason: "  " } }), BOTH)!;
    expect(v.reason).toBe("the server did not say what changed");
    expect(v.why).toBeNull();
  });

  test("no reject control from the server means no Dismiss", () => {
    const v = nudgeView(nudge({ render_status: { sendable: false, reason: "x" } }), {
      approvable: false,
      rejectable: false,
    })!;
    expect(v.canSend || v.canDismiss).toBe(false);
  });
});

describe("what it shows", () => {
  test("the persisted text verbatim, the facts, the objective, and the AI's why apart", () => {
    const v = nudgeView(nudge(), BOTH)!;
    expect(v.text).toBe(TEXT);
    expect(v.objective).toBe("Checks are green on the PR");
    expect(v.source).toBe("direction");
    expect(v.facts.map((f) => f.name)).toEqual(["pr", "checks"]);
    expect(v.why).toBe("The last push broke the lint step.");
  });

  test("a rationale wins over the title, and the key stands in for an unreadable title", () => {
    const v = nudgeView(nudge({ rationale: "Stalled on docs.", objective_title: undefined }), BOTH)!;
    expect(v.why).toBe("Stalled on docs.");
    expect(v.objective).toBe("checks");
  });

  test("a default-nudge render reads as the default nudge", () => {
    const v = nudgeView(nudge({ render: { text: "keep going", source: "default_nudge", facts: [] } }), BOTH)!;
    expect(v.source).toBe("default_nudge");
    expect(v.facts).toEqual([]);
  });
});

describe("provenance", () => {
  const clock = (ts: number) => `T${ts}`;

  test("observed facts name the newest check, the repo and the short head", () => {
    expect(provenance(nudge().render!.facts, clock)).toBe(
      "checked at T120 · acme/app · head 4b7e0d9 · by this objective's own probe, not read from the session",
    );
  });

  test("facts from the probe's own settings say so, and no facts say nothing", () => {
    expect(provenance([{ name: "branch", value: "main", observed_at: null }], clock)).toBe(
      "from this objective's probe settings, not read from the session",
    );
    expect(provenance([], clock)).toBeNull();
  });
});

test("sentence capitalises a server clause and ends it once", () => {
  expect(sentence("the PR head moved")).toBe("The PR head moved.");
  expect(sentence("already a sentence.")).toBe("Already a sentence.");
  expect(sentence("  ")).toBe("");
});
