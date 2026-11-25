import { describe, expect, it } from "vitest";

import type {
  AskOperationCard,
  AskProposalCard,
  AskReliesRow,
  AskSpecialistShown,
} from "../../types/api";
import {
  CREATE_ROWS,
  askAgainText,
  editCounts,
  editRows,
  fieldText,
  foldTally,
  foldable,
  reliesCounts,
  reliesDetail,
  reliesWord,
  shortRev,
  skillDiff,
  skillLines,
  skillText,
  specialistWatch,
} from "./specialistRows";

const REVIEWER: AskSpecialistShown = {
  title: "Code Reviewer",
  summary: "Reviews a pull request before it merges.",
  engine: "claude",
  agent: "Claude Code",
  model: "claude-sonnet-4-5",
  effort: "high",
  grade: "strong",
  skills: [
    { id: "review-checklist", revision: "sha256:11112222aaaa" },
    { id: "security-review", revision: "sha256:33334444bbbb" },
  ],
  capacity: 2,
};

function card(over: Partial<AskOperationCard> = {}): AskOperationCard {
  return {
    action_id: "a".repeat(32),
    kind: "operation",
    op: "specialist.create",
    risk: "reversible",
    label: "New specialist",
    verb: "Create specialist",
    title: "Code Reviewer",
    text: "",
    needs: [],
    state: "pending",
    summary: "",
    reason: "",
    can_confirm: true,
    can_dismiss: true,
    content: true,
    ...over,
  };
}

describe("a new specialist's rows (#1938)", () => {
  it("are the seven fields after the title, in a fixed order", () => {
    expect(CREATE_ROWS).toEqual([
      "summary",
      "agent",
      "model",
      "effort",
      "grade",
      "skills",
      "capacity",
    ]);
  });

  it("say each field as the card does", () => {
    expect(fieldText(REVIEWER, "agent")).toBe("Claude Code");
    expect(fieldText(REVIEWER, "effort")).toBe("High");
    expect(fieldText(REVIEWER, "grade")).toBe("Specialist");
    expect(fieldText(REVIEWER, "capacity")).toBe("Up to 2 at once");
    expect(fieldText({ ...REVIEWER, model: "default" }, "model")).toBe("The agent's default");
    expect(fieldText({ ...REVIEWER, effort: null }, "effort")).toBe("The model's own");
    expect(fieldText({ ...REVIEWER, grade: null }, "grade")).toBe("Ungraded");
    expect(fieldText({ ...REVIEWER, agent: "" }, "agent")).toBe("claude");
  });

  it("show a skill as its id and a short revision", () => {
    expect(shortRev("sha256:11112222aaaa")).toBe("11112222");
    expect(shortRev("r-tdd-1")).toBe("r-tdd-1");
    expect(skillLines(REVIEWER).map(skillText)).toEqual([
      "review-checklist · rev 11112222",
      "security-review · rev 33334444",
    ]);
  });

  it("mark a pin that moved, or a skill that is gone, with what holds now", () => {
    const lines = skillLines(REVIEWER, {
      "review-checklist": "sha256:99990000",
      "security-review": null,
    });
    expect(lines.map(skillText)).toEqual([
      "review-checklist · rev 11112222 · now rev 99990000",
      "security-review · rev 33334444 · no longer in your Library",
    ]);
    // A pin that still holds, and a skill the read said nothing about, are not marked.
    expect(skillLines(REVIEWER, { "review-checklist": "sha256:11112222aaaa" })[0].now).toBe(
      undefined,
    );
  });
});

describe("an edit's per-field diff (#1938)", () => {
  const after: AskSpecialistShown = {
    ...REVIEWER,
    model: "claude-opus-4-5",
    grade: "frontier",
    capacity: 6,
    skills: [...REVIEWER.skills, { id: "test-plan", revision: "sha256:55556666" }],
  };

  it("puts the changed rows first and counts the grade as following, not changed", () => {
    const rows = editRows(REVIEWER, after);
    expect(rows.map((r) => [r.field, r.state])).toEqual([
      ["model", "changed"],
      ["grade", "follows"],
      ["skills", "changed"],
      ["capacity", "changed"],
      ["title", "same"],
      ["summary", "same"],
      ["agent", "same"],
      ["effort", "same"],
    ]);
    expect(editCounts(rows)).toEqual({ changed: 3, follows: 1, unchanged: 4 });
    const model = rows[0];
    expect([model.before, model.after]).toEqual(["claude-sonnet-4-5", "claude-opus-4-5"]);
    expect([rows[1].before, rows[1].after]).toEqual(["Specialist", "Senior"]);
    expect([rows[3].before, rows[3].after]).toEqual(["Up to 2 at once", "Up to 6 at once"]);
  });

  it("leaves the grade unchanged when the new model has the same one", () => {
    const rows = editRows(REVIEWER, { ...REVIEWER, model: "claude-sonnet-5" });
    expect(editCounts(rows)).toEqual({ changed: 1, follows: 0, unchanged: 7 });
  });

  it("signs the skills: kept, removed, added, and a re-pinned one as both", () => {
    const edited: AskSpecialistShown = {
      ...REVIEWER,
      skills: [
        { id: "review-checklist", revision: "sha256:aaaa0000" },
        { id: "test-plan", revision: "sha256:55556666" },
      ],
    };
    expect(skillDiff(REVIEWER, edited).map((k) => `${k.sign}${skillText(k)}`)).toEqual([
      "−review-checklist · rev 11112222",
      "−security-review · rev 33334444",
      "+review-checklist · rev aaaa0000",
      "+test-plan · rev 55556666",
    ]);
    expect(skillDiff(REVIEWER, after).map((k) => k.sign)).toEqual([" ", " ", "+"]);
  });

  it("tells two agents apart by their id, not their name", () => {
    const rows = editRows(REVIEWER, { ...REVIEWER, engine: "codex", agent: "Claude Code" });
    expect(rows[0]).toMatchObject({ field: "agent", state: "changed" });
  });

  it("reads an edit that changes nothing as eight unchanged rows", () => {
    expect(editCounts(editRows(REVIEWER, REVIEWER))).toEqual({
      changed: 0,
      follows: 0,
      unchanged: 8,
    });
  });
});

describe("the specialists a playbook relies on (#1938)", () => {
  const rows: AskReliesRow[] = [
    { title: "Release Notes Writer", state: "proposed", where: "below" },
    {
      title: "Code Reviewer",
      state: "exists",
      version: 4,
      engine: "claude",
      agent: "Claude Code",
      model: "claude-opus-4-5",
    },
    { title: "QA Runner", state: "missing" },
  ];

  it("are counted by state", () => {
    expect(reliesCounts(rows)).toEqual({ named: 3, exists: 1, proposed: 1, missing: 1 });
  });

  it("say where a proposed one's card is, what an existing one runs on, and that one is missing", () => {
    expect(rows.map(reliesWord)).toEqual(["Proposed below", "Exists", "Missing"]);
    expect(rows.map(reliesDetail)).toEqual([
      "Its card is below in this conversation. Not created yet.",
      "Claude Code · claude-opus-4-5",
      "No specialist has this job yet, and none is proposed.",
    ]);
    expect(reliesWord({ title: "x", state: "proposed", where: "above" })).toBe("Proposed above");
  });
});

describe("folding several specialist cards of one turn (#1938)", () => {
  const six = Array.from({ length: 6 }, (_, i) =>
    card({ action_id: String(i).repeat(32), title: `Specialist ${i}` }),
  );

  it("folds them all and gives each its ordinal", () => {
    expect([...foldable(six).values()]).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("never folds a single specialist card", () => {
    expect(foldable([six[0]]).size).toBe(0);
  });

  it("folds only cards that carry a whole specialist", () => {
    const mixed: AskProposalCard[] = [
      card({ op: "playbook.create" }),
      card({ action_id: "b".repeat(32), op: "specialist.archive" }),
      card({ action_id: "c".repeat(32), op: "specialist.unarchive" }),
      card({ action_id: "d".repeat(32), op: "specialist.edit" }),
    ];
    expect(foldable(mixed).size).toBe(0); // one edit: shown whole
    const two = [...mixed, card({ action_id: "e".repeat(32) })];
    expect([...foldable(two)]).toEqual([
      ["d".repeat(32), 1],
      ["e".repeat(32), 2],
    ]);
  });

  it("tallies waiting and decided cards", () => {
    const some = [six[0], { ...six[1], state: "done" as const }, { ...six[2], state: "stale" as const }];
    expect(foldTally(some)).toEqual({ waiting: 1, decided: 2 });
  });
});

describe("what a playbook card's list follows, and Ask HQ again (#1938)", () => {
  it("changes whenever a specialist card's state does, and with nothing else", () => {
    const a = card();
    const pb = card({ action_id: "b".repeat(32), op: "playbook.create" });
    const before = specialistWatch([pb, a]);
    expect(specialistWatch([{ ...pb, state: "done" }, a])).toBe(before);
    expect(specialistWatch([pb, { ...a, state: "dismissed" }])).not.toBe(before);
  });

  it("asks for the same card again, by its action and target", () => {
    expect(askAgainText(card())).toBe(
      "The card “New specialist: Code Reviewer” is out of date. Propose it again as it stands now.",
    );
  });
});
