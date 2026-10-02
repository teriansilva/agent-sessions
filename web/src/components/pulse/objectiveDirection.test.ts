/** Which op the Edit direction dialog sends for each choice (#983 P2). */
import { describe, expect, test } from "vitest";

import type { MissionObjective } from "../../types/api";

import { hasDirection, initialChoice, isCopied, opFor } from "./objectiveDirection";

const obj = (over: Partial<MissionObjective> = {}) =>
  ({ key: "checks", direction: null, direction_source: null, ...over }) as MissionObjective;

const COPIED = obj({ direction: "PR #{pr} is {checks}", direction_source: "template" });
const OWN = obj({ direction: "Fix the flaky test", direction_source: "operator" });
const NONE = obj();

describe("the starting choice is what the objective has now", () => {
  test("a playbook copy starts on keep, the mission's own on write, none on none", () => {
    expect(initialChoice(COPIED)).toBe("keep");
    expect(initialChoice(OWN)).toBe("write");
    expect(initialChoice(NONE)).toBe("none");
  });

  test("whitespace is no direction, and a direction with no source is the mission's own", () => {
    expect(hasDirection(obj({ direction: "   " }))).toBe(false);
    expect(initialChoice(obj({ direction: " \n", direction_source: "template" }))).toBe("none");
    expect(isCopied(obj({ direction: "x", direction_source: null }))).toBe(false);
    expect(initialChoice(obj({ direction: "x", direction_source: null }))).toBe("write");
  });
});

describe("opFor", () => {
  test("keep never sends anything", () => {
    expect(opFor("keep", COPIED, "anything")).toBeNull();
  });

  test("write sends set_direction with the text exactly as typed", () => {
    const text = "  PR #{pr}: fix the test,\nnot the timeout  ";
    expect(opFor("write", COPIED, text)).toEqual({
      op: "set_direction",
      key: "checks",
      direction: text,
    });
    expect(opFor("write", NONE, "x")).toEqual({ op: "set_direction", key: "checks", direction: "x" });
  });

  test("writing the playbook's own words still makes it this mission's direction", () => {
    expect(opFor("write", COPIED, COPIED.direction as string)).toEqual({
      op: "set_direction",
      key: "checks",
      direction: COPIED.direction,
    });
  });

  test("write sends nothing for empty text or for the mission's unchanged direction", () => {
    expect(opFor("write", NONE, "   ")).toBeNull();
    expect(opFor("write", OWN, OWN.direction as string)).toBeNull();
  });

  test("none clears a direction that exists and sends nothing when there is none", () => {
    expect(opFor("none", COPIED, "")).toEqual({ op: "clear_direction", key: "checks" });
    expect(opFor("none", OWN, "")).toEqual({ op: "clear_direction", key: "checks" });
    expect(opFor("none", NONE, "")).toBeNull();
  });
});
