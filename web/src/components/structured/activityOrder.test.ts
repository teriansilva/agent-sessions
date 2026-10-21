import { describe, expect, test } from "vitest";
import type { StructuredTool } from "../../types/api";
import { interleave } from "./activityOrder";

const call = (id: string, at?: number): StructuredTool => ({
  id, name: "Bash", outcome: "completed", summary: id, ...(at === undefined ? {} : { at }),
});
const shape = (reply: string, tools: StructuredTool[]) => {
  const order = interleave(reply, tools);
  return order && {
    lead: order.lead.map((t) => t.id),
    parts: order.parts.map((p) => [p.text.trim(), p.tools.map((t) => t.id)]),
  };
};

describe("a turn's activity reads in the order it ran", () => {
  const reply = "Reading the launch path.\n\nNow the decide module.\n\nAll tests pass.";
  const second = reply.indexOf("Now");
  const third = reply.indexOf("All");

  test("calls sit after the text written before them, and the newest after all of it", () => {
    expect(shape(reply, [
      call("a", 0), call("b", second - 2), call("c", second - 2), call("d", third - 2), call("e", reply.length),
    ])).toEqual({
      lead: ["a"],
      parts: [
        ["Reading the launch path.", ["b", "c"]],
        ["Now the decide module.", ["d"]],
        ["All tests pass.", ["e"]],
      ],
    });
  });

  test("a reply written after every call keeps all of them before it", () => {
    expect(shape(reply, [call("a", 0), call("b", 0)])).toEqual({ lead: ["a", "b"], parts: [[reply, []]] });
  });

  test("blank text between two calls never makes an empty message", () => {
    const text = "One.\n\n\n\nTwo.";
    expect(shape(text, [call("a", 4), call("b", 6)])).toEqual({
      lead: [], parts: [["One.", ["a", "b"]], ["Two.", []]],
    });
  });

  test("a place past the reply (a cut reply) clamps to its end", () => {
    expect(shape("Short.", [call("a", 99)])).toEqual({ lead: [], parts: [["Short.", ["a"]]] });
  });

  test("a call without a place, or no reply, keeps the single block before the reply", () => {
    expect(interleave(reply, [call("a", 0), call("b")])).toBeNull();
    expect(interleave("", [call("a", 0)])).toBeNull();
    expect(interleave(null, [])).toBeNull();
  });
});
