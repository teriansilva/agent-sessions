import { describe, expect, test } from "vitest";
import type { StructuredAgentTurn, StructuredTurn } from "../../types/api";
import { conversationGroups } from "./conversationOrder";

const turn = (id: string, extra: Partial<StructuredTurn> = {}): StructuredTurn => ({
  turn_id: id, operation_id: id, state: "running", text: id, reply: "Response",
  text_truncated: false, reply_truncated: false, reason: null, tools: [], tools_truncated: false, ...extra,
});
const addition = (id: string, target: string, extra: Partial<StructuredTurn> = {}) =>
  turn(id, { state: "delivered", delivery: "steered", delivery_turn_id: target, reply: null, ...extra });
const order = (turns: (StructuredTurn | StructuredAgentTurn)[]) => conversationGroups(turns)
  .map(({ turn, additions }) => [turn.turn_id, additions.map((t) => t.turn_id)]);

describe("acknowledged additions belong before their recorded response (#1479)", () => {
  test("groups multiple additions in snapshot order without changing source records", () => {
    const active = turn("active");
    const first = addition("first", "active");
    const second = addition("second", "active");
    const queued = turn("queued", { state: "queued" });
    const turns = [active, first, second, queued];
    const original = structuredClone(turns);
    const groups = conversationGroups(turns);
    expect(order(turns)).toEqual([["active", ["first", "second"]], ["queued", []]]);
    expect(groups[0].turn).toBe(active);
    expect(groups[0].additions).toEqual([first, second]);
    expect(turns).toEqual(original);
  });

  test.each(["running", "completed", "interrupted", "failed"])("keeps the recorded %s target when a newer turn starts", (state) => {
    expect(order([turn("old", { state }), addition("added", "old"), turn("new")]))
      .toEqual([["old", ["added"]], ["new", []]]);
  });

  test.each(["queued", "delivering", "uncertain", "failed", "interrupted", "running", "completed"])(
    "does not regroup a %s message on the target ID alone", (state) => {
      expect(order([turn("target"), addition("message", "target", { state })]))
        .toEqual([["target", []], ["message", []]]);
    },
  );

  test.each([undefined, "steering", "interrupting", "interrupt_failed"] as const)(
    "requires the acknowledged steer marker, not %s", (delivery) => {
      expect(order([turn("target"), addition("message", "target", { delivery })]))
        .toEqual([["target", []], ["message", []]]);
    },
  );

  test("keeps missing targets and old records without a target visible", () => {
    expect(order([turn("other"), addition("missing", "omitted"), addition("legacy", "", { delivery_turn_id: undefined })]))
      .toEqual([["other", []], ["missing", []], ["legacy", []]]);
  });

  test("does not create recursive groups from self references, forward references or other additions", () => {
    expect(order([addition("self", "self"), addition("early", "later"), turn("later"),
      addition("first", "later"), addition("second", "first")]))
      .toEqual([["self", []], ["early", []], ["later", ["first"]], ["second", []]]);
  });

  test("keeps messages bound to their own target without inferring from timestamps", () => {
    expect(order([turn("a", { ts: 100 }), addition("a1", "a", { ts: 90 }),
      turn("b", { ts: 90 }), addition("b1", "b", { ts: 80 })]))
      .toEqual([["a", ["a1"]], ["b", ["b1"]]]);
  });

  test("can group an explicitly acknowledged addition with an agent-owned target", () => {
    const own: StructuredAgentTurn = { kind: "agent", turn_id: "own", state: "running",
      reply: "Working", reply_truncated: false, tools: [], tools_truncated: false };
    expect(order([own, addition("added", "own")])).toEqual([["own", ["added"]]]);
  });
});
