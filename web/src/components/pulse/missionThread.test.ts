/** The thread's event → row mapping and the Start again eligibility rule (#967 P4, #966 P2).
 *
 *  Every kind the server writes is pinned here, plus what happens to one it does not know: a generic
 *  row with a humanised label and its own text, never its meta. */
import { describe, expect, test } from "vitest";

import type { Mission, MissionEvent } from "../../types/api";

import {
  canStartAgain,
  isFailedStart,
  latestFailedStartSeq,
  sessionRoute,
  threadRow,
} from "./missionThread";

function ev(
  kind: string,
  text: string | null = null,
  meta: Record<string, unknown> | null = null,
  over: Partial<MissionEvent> = {},
): MissionEvent {
  return {
    seq: 1,
    mission_id: "msn_1",
    at: 1,
    kind,
    session_key: null,
    action_id: null,
    text,
    meta,
    settlement: null,
    ...over,
  };
}

function mission(over: Partial<Mission> = {}): Mission {
  return {
    id: "msn_1",
    title: "t",
    instruction: null,
    brief: null,
    project_id: "p1",
    cwd: "/repo",
    engine: null,
    engine_source: null,
    state: "failed",
    playbook_id: null,
    created_at: 1,
    updated_at: 1,
    closed_at: null,
    archived_at: null,
    archiving_at: null,
    unarchiving_at: null,
    outcome: null,
    sessions: [],
    ...over,
  };
}

describe("conversation", () => {
  test("operator and assistant messages stay messages", () => {
    expect(threadRow(ev("operator_msg", "hi"))).toEqual({ type: "message", who: "You" });
    expect(threadRow(ev("assistant_msg", "hello"))).toEqual({ type: "message", who: "Answer" });
  });
});

describe("state", () => {
  test("a state event with NO text is still a from → to pair", () => {
    expect(threadRow(ev("state", null, { from: "draft", to: "planned", released: [] }))).toEqual({
      type: "state",
      from: "draft",
      to: "planned",
      note: null,
    });
  });

  test("the settlement's `a -> b` text is not repeated beside the chips", () => {
    expect(
      threadRow(ev("state", "dispatching -> running", { from: "dispatching", to: "running" })),
    ).toMatchObject({ type: "state", note: null });
  });

  test("a why, then a detail, then the caller's own text is the note", () => {
    expect(
      threadRow(ev("state", "t", { from: "running", to: "review", why: "w", detail: "d" })),
    ).toMatchObject({ note: "w" });
    expect(threadRow(ev("state", "t", { from: "running", to: "review", detail: "d" }))).toMatchObject({
      note: "d",
    });
    expect(threadRow(ev("state", "operator reopened", { from: "done", to: "running" }))).toMatchObject({
      note: "operator reopened",
    });
  });

  test("a state event that lost its from or to is a generic row, not a half pair", () => {
    expect(threadRow(ev("state", null, { to: "planned" }))).toEqual({
      type: "system",
      label: "State",
      text: null,
    });
  });

  test("failing a running mission is a state change, not a failed start", () => {
    const e = ev("state", null, { from: "running", to: "failed" });
    expect(isFailedStart(e)).toBe(false);
    expect(threadRow(e)).toMatchObject({ type: "state", from: "running", to: "failed" });
  });
});

describe("a start that failed", () => {
  const meta = {
    from: "dispatching",
    to: "failed",
    detail: "the session never became ready (first-paint never true)",
    session_key: "claude:abc",
    seed_outcome: "not_attempted",
    teardown_confirmed: true,
    retry_eligible: true,
    message: "The session never became ready, so nothing was typed. Start again restores the plan.",
  };

  test("carries the plain-language message, the technical detail and the session", () => {
    expect(threadRow(ev("state", `dispatching -> failed: ${meta.detail}`, meta))).toEqual({
      type: "failure",
      from: "dispatching",
      to: "failed",
      message: meta.message,
      detail: meta.detail,
      sessionKey: "claude:abc",
    });
  });

  test("without a message (an older event) the text after the transition is the message", () => {
    const row = threadRow(
      ev("state", "dispatching -> failed: it broke", { from: "dispatching", to: "failed" }),
    );
    expect(row).toMatchObject({ type: "failure", message: "it broke", detail: null });
  });

  test("with nothing at all it still says the start failed", () => {
    expect(threadRow(ev("state", null, { from: "dispatching", to: "failed" }))).toMatchObject({
      type: "failure",
      message: "The start failed.",
    });
  });

  test("a detail identical to the message is not printed twice", () => {
    const row = threadRow(
      ev("state", null, { from: "dispatching", to: "failed", message: "x", detail: "x" }),
    );
    expect(row).toMatchObject({ message: "x", detail: null });
  });

  test("the event's own session wins over the one in meta", () => {
    const row = threadRow(
      ev(
        "state",
        null,
        { from: "dispatching", to: "failed", session_key: "claude:meta" },
        { session_key: "codex:1" },
      ),
    );
    expect(row).toMatchObject({ type: "failure", sessionKey: "codex:1" });
  });

  describe("the session link (PR #986 review)", () => {
    const failed = { from: "dispatching", to: "failed" };
    const key = "claude:f48fde5a-1111-2222-3333-444444444444";

    test("the producer's launch_session_key comes FIRST", () => {
      const row = threadRow(
        ev(
          "state",
          null,
          { ...failed, launch_session_key: key, session_key: "codex:meta" },
          { session_key: "codex:event" },
        ),
      );
      expect(row).toMatchObject({ type: "failure", sessionKey: key });
    });

    test("an older event with no launch_session_key falls back to its own keys", () => {
      expect(
        threadRow(ev("state", null, failed, { session_key: "codex:event" })),
      ).toMatchObject({ sessionKey: "codex:event" });
      expect(threadRow(ev("state", null, { ...failed, session_key: "codex:meta" }))).toMatchObject({
        sessionKey: "codex:meta",
      });
    });

    test("no key at all is no link, and a neighbouring session event is never consulted", () => {
      expect(threadRow(ev("state", null, failed))).toMatchObject({ sessionKey: null });
    });

    test.each([
      ["a path", "claude:../../etc"],
      ["a launch placeholder", "opencode:new-11111111-2222-3333-4444-555555555555"],
      ["no engine", "f48fde5a"],
      ["a non-string", 42],
    ])("%s is not a link target; the next candidate is used", (_label, bad) => {
      expect(
        threadRow(ev("state", null, { ...failed, launch_session_key: bad })),
      ).toMatchObject({ sessionKey: null });
      expect(
        threadRow(
          ev("state", null, { ...failed, launch_session_key: bad }, { session_key: "codex:event" }),
        ),
      ).toMatchObject({ sessionKey: "codex:event" });
    });
  });

  test("the newest failure is chosen by seq, not by position", () => {
    const events = [
      ev("state", null, { from: "dispatching", to: "failed" }, { seq: 3 }),
      ev("state", null, { from: "dispatching", to: "failed" }, { seq: 9 }),
      ev("state", null, { from: "planned", to: "dispatching" }, { seq: 12 }),
      ev("operator_msg", "x", null, { seq: 13 }),
      // Newer than every failure, and not one: a question notice never becomes the actionable block.
      ev("error", "no question could be asked", null, { seq: 14 }),
    ];
    expect(latestFailedStartSeq(events)).toBe(9);
    expect(latestFailedStartSeq(events.reverse())).toBe(9);
    expect(latestFailedStartSeq([ev("operator_msg", "x")])).toBeNull();
  });
});

describe("an error event", () => {
  // Its only server writer is `mission_questions._notice`: a question that could not be asked or
  // delivered. That is not a start failure, whatever the mission's state.
  test("is an error row with its own text, never a failure block", () => {
    const e = ev(
      "error",
      "No question could be asked: the reply offered no usable options.",
      { from: "dispatching", to: "failed", detail: "409", message: "not this" },
      { session_key: "codex:1" },
    );
    expect(isFailedStart(e)).toBe(false);
    expect(threadRow(e)).toEqual({
      type: "error",
      text: "No question could be asked: the reply offered no usable options.",
    });
  });

  test("with no text it still says an error was recorded, and prints no meta", () => {
    const row = threadRow(ev("error", "  ", { detail: "do not print" }));
    expect(row).toEqual({ type: "error", text: "An error was recorded." });
    expect(JSON.stringify(row)).not.toContain("do not print");
  });
});

describe("plans", () => {
  test("a plan is its project, engine and brief", () => {
    expect(
      threadRow(
        ev("plan", "Fix it", {
          plan_id: "pln_1",
          project_id: "p1",
          engine: "claude",
          engine_reason: "why",
          generation: 1,
        }),
      ),
    ).toEqual({ type: "plan", projectId: "p1", engine: "claude", brief: "Fix it" });
  });

  test("a plan edit names the changed fields in the operator's words, and never the brief", () => {
    expect(
      threadRow(ev("plan_edit", null, { plan_id: "pln_2", changed: ["project_id", "engine", "brief", 7] })),
    ).toEqual({ type: "plan_edit", changed: ["project", "engine", "brief"] });
    expect(threadRow(ev("plan_edit", null, { plan_id: "pln_2" }))).toEqual({
      type: "plan_edit",
      changed: [],
    });
  });

  test.each([
    ["skipped", "no plan proposed: no AI endpoint is configured", "No plan proposed", "no AI endpoint is configured"],
    ["failed", "could not plan: the model timed out", "Couldn't plan", "the model timed out"],
    ["failed", "could not plan", "Couldn't plan", null],
    [
      "discarded",
      "a plan result was discarded: planning attempt 1 is no longer current (attempt 2 is pending)",
      "Planning result discarded",
      "a plan result was discarded: planning attempt 1 is no longer current (attempt 2 is pending)",
    ],
    [
      "recovered",
      "the plan for attempt 2 was already stored; settled without another model call",
      "Plan recovered",
      "the plan for attempt 2 was already stored; settled without another model call",
    ],
    ["project_conflict", "the planner named another project; kept Alpha", "Project kept", "the planner named another project; kept Alpha"],
    ["something_new", "text", "Planning", "text"],
  ])("planning %s: %s", (outcome, text, label, note) => {
    expect(threadRow(ev("planning", text, { outcome, generation: 1 }))).toEqual({
      type: "planning",
      outcome,
      label,
      note,
    });
  });
});

describe("anything else", () => {
  test("an unknown kind is a generic row: humanised label, its text, never its meta", () => {
    const row = threadRow(ev("objective_waived", "  ", { secret: "do not print" }));
    expect(row).toEqual({ type: "system", label: "Objective waived", text: null });
    expect(JSON.stringify(row)).not.toContain("do not print");
    expect(threadRow(ev("session", "claude:1 joined"))).toEqual({
      type: "system",
      label: "Session",
      text: "claude:1 joined",
    });
  });

  test("a malformed meta never throws", () => {
    for (const kind of ["state", "plan", "plan_edit", "planning", "error", "x"]) {
      expect(() =>
        threadRow(ev(kind, null, "nope" as unknown as Record<string, unknown>)),
      ).not.toThrow();
      expect(() => threadRow(ev(kind, null, { changed: "engine", from: 3, to: {} }))).not.toThrow();
    }
  });

  test("a session route encodes both halves", () => {
    expect(sessionRoute("claude:a/b")).toBe("/s/claude/a%2Fb");
  });
});

describe("Start again is decided by the DETAIL", () => {
  test("eligible only for a failed, unarchived mission whose detail says so", () => {
    expect(canStartAgain(mission({ retry_eligible: true }))).toBe(true);
    expect(canStartAgain(mission({ retry_eligible: false, retry_reason: "why" }))).toBe(false);
    expect(canStartAgain(mission({}))).toBe(false);
    expect(canStartAgain(mission({ state: "planned", retry_eligible: true }))).toBe(false);
    expect(canStartAgain(mission({ retry_eligible: true, archived_at: 5 }))).toBe(false);
    expect(canStartAgain(null)).toBe(false);
  });

  test("an event snapshot saying eligible does not make an ineligible mission eligible", () => {
    const snapshot = ev("state", null, {
      from: "dispatching",
      to: "failed",
      retry_eligible: true,
      seed_outcome: "not_attempted",
    });
    expect(
      canStartAgain(mission({ retry_eligible: false, events: [snapshot] })),
    ).toBe(false);
  });
});
