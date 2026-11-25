import { describe, expect, test } from "vitest";
import type { StructuredTool } from "../../types/api";
import { fold, interleave } from "./activityOrder";

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

describe("message boundaries (#1643)", () => {
  const notes = ["One.", "A list:\n\n- a\n\n- b", "Three."];
  const reply = notes.join("\n\n");
  const breaks = [0, reply.indexOf("A list"), reply.indexOf("Three")];
  const messages = (r: string, tools: StructuredTool[], b?: number[]) =>
    interleave(r, tools, b)!.parts.map((p) => [p.message, p.text.trim(), p.tools.map((t) => t.id)]);

  test("text-only progress is one part per message, blank lines inside one stay inside", () => {
    expect(messages(reply, [], breaks)).toEqual([
      [0, "One.", []], [1, notes[1], []], [2, "Three.", []],
    ]);
  });

  test("a call between two messages follows the earlier one", () => {
    expect(messages(reply, [call("x", breaks[1]), call("y", reply.indexOf("- b"))], breaks)).toEqual([
      [0, "One.", ["x"]], [1, "A list:\n\n- a", ["y"]], [1, "- b", []], [2, "Three.", []],
    ]);
  });

  test("without boundaries nothing changes", () => {
    expect(messages(reply, [], undefined)).toEqual([[0, reply, []]]);
  });

  const long = Array.from({ length: 12 }, (_, i) => `Update ${i}.`);
  const longReply = long.join("\n\n");
  const longBreaks = long.map((n) => longReply.indexOf(n));

  test("over eight messages, all but the newest three fold, calls with them", () => {
    const tools = [call("early", longBreaks[2]), call("late", longBreaks[11])];
    const order = interleave(longReply, tools, longBreaks)!;
    const f = fold(order.parts)!;
    expect(f.count).toBe(9);
    expect(f.folded.map((p) => p.text.trim())).toEqual(long.slice(0, 9));
    expect(f.folded.flatMap((p) => p.tools.map((t) => t.id))).toEqual(["early"]);
    expect(f.shown.map((p) => p.text.trim())).toEqual(long.slice(9));
    expect(f.shown.flatMap((p) => p.tools.map((t) => t.id))).toEqual(["late"]);
  });

  test("eight messages or fewer never fold", () => {
    const eight = long.slice(0, 8).join("\n\n");
    const b = long.slice(0, 8).map((n) => eight.indexOf(n));
    expect(fold(interleave(eight, [], b)!.parts)).toBeNull();
  });
});
