import { describe, expect, test } from "vitest";
import { memorySentence, oomKillSentence } from "./memoryPressure";

const GIB = 1024 ** 3;

describe("OOM notices (#1732)", () => {
  test("a count, never a process", () => {
    expect(oomKillSentence(1)).toBe("A process in this session ran out of memory and was killed.");
    expect(oomKillSentence(3)).toBe("3 processes in this session ran out of memory and were killed.");
  });

  test("near the cap, the banner says one process is killed, not that the session stops", () => {
    const text = memorySentence({ level: "critical", working_set: 7.6 * GIB, max: 8 * GIB });
    expect(text).toContain("At 8 GB the kernel kills one of its processes, usually the largest.");
    expect(text).not.toContain("it stops");
  });
});
