/** The thread's supervisor-nudge rows (#983 P2): delivered, held, legacy, and a direction that could
 *  not be filled. Every fixture is the server's own shape: `missions.ensure_delivered_nudge_event`,
 *  `ensure_held_event` (and its pre-stage form), and `escalate_once` with `meta.held: "direction"`. */
import { describe, expect, test } from "vitest";

import type { MissionEvent } from "../../types/api";

import { threadRow } from "./missionThread";

function ev(
  kind: string,
  text: string | null,
  meta: Record<string, unknown> | null,
): MissionEvent {
  return {
    seq: 1,
    mission_id: "msn_1",
    at: 1,
    kind,
    session_key: "claude:s",
    action_id: "act_1",
    text,
    meta,
    settlement: null,
  };
}

const TYPED = "PR #412's checks are failing on fix/upload-retry.\n  Open the failing check.  ";

describe("a delivered nudge", () => {
  test("is a Nudged row carrying the typed snapshot verbatim and whose words it was", () => {
    const row = threadRow(
      ev("action", TYPED, {
        source: "supervisor",
        objective_key: "checks",
        episode: 1,
        delivered: true,
        text_source: "direction",
        digest: "d",
        stage: "delivered",
      }),
    );
    expect(row).toEqual({
      type: "nudged",
      objectiveKey: "checks",
      source: "direction",
      text: TYPED,
    });
  });

  test("the default nudge is named, and an unknown source names nothing", () => {
    const meta = { source: "supervisor", objective_key: "review", stage: "delivered" };
    expect(threadRow(ev("action", "keep going", { ...meta, text_source: "default_nudge" }))).toMatchObject({
      type: "nudged",
      source: "default_nudge",
    });
    expect(threadRow(ev("action", "keep going", { ...meta, text_source: "made up" }))).toMatchObject({
      type: "nudged",
      source: null,
    });
    expect(threadRow(ev("action", null, meta))).toMatchObject({ type: "nudged", text: "" });
  });
});

describe("a nudge that was not typed", () => {
  test("a held stage is a Held row with the server's reason, without its lead-in", () => {
    expect(
      threadRow(
        ev("action", "A nudge was prepared but not delivered: session is not live", {
          source: "supervisor",
          objective_key: "checks",
          episode: 2,
          held: true,
          state: "failed",
          stage: "held",
        }),
      ),
    ).toEqual({ type: "held", objectiveKey: "checks", reason: "session is not live" });
  });

  test("a LEGACY action event with no stage reads as held", () => {
    expect(
      threadRow(
        ev("action", "A nudge was prepared but not delivered: the delivery settled as stale", {
          source: "supervisor",
          objective_key: "pr",
          held: true,
          state: "stale",
        }),
      ),
    ).toEqual({ type: "held", objectiveKey: "pr", reason: "the delivery settled as stale" });
  });

  test("a held event with no reason still says something", () => {
    expect(
      threadRow(ev("action", "A nudge was prepared but not delivered:  ", { source: "supervisor" })),
    ).toEqual({ type: "held", objectiveKey: null, reason: "it was not sent" });
  });

  test("a direction that could not be filled is Held with the reason after the title", () => {
    const row = threadRow(
      ev(
        "escalation",
        "Checks are green: on the PR: its direction could not be filled: {pr} is missing from this objective's latest observation",
        { held: "direction", objective_key: "checks", episode: 1 },
      ),
    );
    expect(row).toEqual({
      type: "held",
      objectiveKey: "checks",
      reason:
        "its direction could not be filled: {pr} is missing from this objective's latest observation",
    });
  });
});

describe("an AI-drafted direction (#983 P3)", () => {
  test("a delivered draft is a Nudged row naming it as the AI's draft", () => {
    const row = threadRow(
      ev("action", TYPED, {
        source: "supervisor",
        objective_key: "review",
        episode: 1,
        delivered: true,
        text_source: "ai_draft",
        digest: null,
        stage: "delivered",
      }),
    );
    expect(row).toEqual({ type: "nudged", objectiveKey: "review", source: "ai_draft", text: TYPED });
  });

  test("a dismissed or replaced draft is a draft-not-sent row with the reason", () => {
    const meta = {
      source: "supervisor",
      objective_key: "review",
      episode: 1,
      held: true,
      state: "rejected",
      draft: true,
      stage: "held",
    };
    expect(
      threadRow(ev("action", "An AI-drafted direction was not sent: you dismissed it", meta)),
    ).toEqual({ type: "held", objectiveKey: "review", reason: "you dismissed it", draft: true });
    expect(
      threadRow(
        ev("action", "An AI-drafted direction was not sent: you sent your own edit instead", meta),
      ),
    ).toEqual({
      type: "held",
      objectiveKey: "review",
      reason: "you sent your own edit instead",
      draft: true,
    });
  });
});

describe("an autonomously sent AI direction (#983 P4)", () => {
  const AUTO = {
    source: "supervisor",
    objective_key: "review",
    episode: 1,
    delivered: true,
    text_source: "ai_auto",
    auto: true,
    digest: null,
    stage: "delivered",
  };

  test("is its OWN source, carrying the confidence — not a flag on the operator-sent one", () => {
    expect(threadRow(ev("action", TYPED, { ...AUTO, confidence: 0.97 }))).toEqual({
      type: "nudged",
      objectiveKey: "review",
      source: "ai_auto",
      text: TYPED,
      confidence: 0.97,
    });
  });

  test("a confidence that is not a finite number is left off rather than rendered", () => {
    // `typeof NaN === "number"`, so the finite test is what stops the row reading "NaN".
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, "0.9", null]) {
      expect(threadRow(ev("action", TYPED, { ...AUTO, confidence: bad }))).toEqual({
        type: "nudged",
        objectiveKey: "review",
        source: "ai_auto",
        text: TYPED,
      });
    }
  });

  test("a draft the OPERATOR sent never carries a confidence, even if one is on the event", () => {
    expect(
      threadRow(
        ev("action", TYPED, { ...AUTO, text_source: "ai_draft", auto: undefined, confidence: 0.97 }),
      ),
    ).toEqual({ type: "nudged", objectiveKey: "review", source: "ai_draft", text: TYPED });
  });
});

describe("what stays as it was", () => {
  test("an ordinary escalation is not a Held row", () => {
    expect(
      threadRow(ev("escalation", "the budget is spent", { objective_key: "checks", episode: 1 })),
    ).toEqual({ type: "system", label: "Escalation", text: "the budget is spent" });
  });

  test("an action event from anything but the supervisor is a generic row, and its meta is never read out", () => {
    const row = threadRow(ev("action", "relayed", { source: "operator", stage: "delivered", secret: "x" }));
    expect(row).toEqual({ type: "system", label: "Action", text: "relayed" });
  });
});
