import { describe, expect, it } from "vitest";

import type { ActionEntry, ActionGallery } from "../types/actions";
import {
  actionMatches,
  categoryChips,
  comingCategories,
  effectOf,
  galleryProblem,
  offeredActions,
  requirementLine,
  usedByWords,
  visibleActions,
} from "./actions";

const ALL = ["once", "schedule", "manual", "webhook"];

function entry(over: Partial<ActionEntry>): ActionEntry {
  return {
    id: "tell_hq",
    label: "Tell HQ",
    category: "battlelab",
    description: "Puts a message in your notifications.",
    effect: "notify",
    executor: "tell_hq",
    triggers: ALL,
    inputs: [],
    outputs: [],
    requires: [],
    available: true,
    used_by: 0,
    ...over,
  };
}

const G: ActionGallery = {
  actions: [
    entry({ id: "start_mission", label: "Start a mission", effect: "write-battlelab", triggers: ["once", "schedule", "manual"] }),
    entry({
      id: "start_session",
      label: "Start a session",
      effect: "write-battlelab",
      requires: [{ id: "api_agent", words: "an API agent", ok: false, detail: "needs an API agent" }],
      available: false,
    }),
    entry({
      id: "send_to_session",
      label: "Message a session",
      requires: [{ id: "running_session", words: "a running API session", ok: null, detail: "" }],
    }),
    entry({ id: "forge_review_loop", label: "Review loop", category: "forge", triggers: ["webhook"] }),
    entry({ id: "tell_hq", label: "Tell HQ", used_by: 3 }),
  ],
  categories: [
    { id: "battlelab", label: "BattleLab" },
    { id: "messaging", label: "Messaging" },
    { id: "forge", label: "Forge" },
  ],
  effects: [
    { id: "read", level: 1, label: "Read" },
    { id: "notify", level: 2, label: "Notify" },
    { id: "write-battlelab", level: 3, label: "Write · BattleLab" },
  ],
};
const KNOWN = ["start_mission", "start_session", "send_to_session", "forge_review_loop", "tell_hq"];

describe("the action gallery's rules (#2248 P1)", () => {
  it("searches the name, the line and the category's name", () => {
    expect(actionMatches(G.actions[0], "MISSION")).toBe(true);
    expect(actionMatches(G.actions[0], "notifications")).toBe(true);
    expect(actionMatches(G.actions[3], "forge", "Forge")).toBe(true);
    expect(actionMatches(G.actions[0], "zebra")).toBe(false);
    expect(visibleActions(G, "forge", "all").map((a) => a.id)).toEqual(["forge_review_loop"]);
    expect(visibleActions(G, "", "forge").map((a) => a.id)).toEqual(["forge_review_loop"]);
  });

  it("chips only the categories that have an action, counting what the search leaves", () => {
    expect(categoryChips(G, "")).toEqual([
      { value: "all", label: "All", n: 5 },
      { value: "battlelab", label: "BattleLab", n: 4 },
      { value: "forge", label: "Forge", n: 1 },
    ]);
    expect(categoryChips(G, "session").map((c) => c.n)).toEqual([2, 2, 0]);
    expect(comingCategories(G)).toEqual(["Messaging"]);
  });

  it("draws an effect as its level and words", () => {
    expect(effectOf(G, "write-battlelab")).toEqual({ level: 3, label: "Write · BattleLab" });
    expect(effectOf(null, "read")).toEqual({ level: 0, label: "read" });
  });

  it("says Ready, what still depends on the wizard, or the first unmet need", () => {
    expect(requirementLine(G.actions[4])).toEqual({ tone: "up", word: "Ready", extra: "" });
    expect(requirementLine(G.actions[2])).toEqual({ tone: "up", word: "Ready", extra: "needs a running API session" });
    expect(requirementLine(G.actions[1])).toEqual({ tone: "degraded", word: "Needs an API agent", extra: "" });
    expect(usedByWords(3)).toBe("used by 3");
    expect(usedByWords(0)).toBe("not used yet");
    expect(usedByWords(null)).toBe("use unknown");
  });

  it("offers what the trigger starts, in the gallery's order, as the fixed cards did", () => {
    const ids = (t: string, legacy = false) => offeredActions(G.actions, t, legacy, KNOWN).map((a) => a.id);
    expect(ids("schedule")).toEqual(["start_mission", "start_session", "send_to_session", "tell_hq"]);
    expect(ids("manual")).toEqual(["start_mission", "start_session", "send_to_session", "tell_hq"]);
    expect(ids("webhook")).toEqual(["start_session", "send_to_session", "forge_review_loop", "tell_hq"]);
    expect(ids("webhook", true)).toEqual(["forge_review_loop"]);
    // An action this build has no fields for is never offered.
    const more = [...G.actions, entry({ id: "run_script", label: "Run a script" })];
    expect(offeredActions(more, "manual", false, KNOWN).map((a) => a.id)).not.toContain("run_script");
  });

  it("holds the What step until a current gallery answer, a failed or pending refresh included", () => {
    expect(galleryProblem({ gallery: null, error: "", loading: true })).toBe("Loading the actions…");
    expect(galleryProblem({ gallery: null, error: "down", loading: false })).toMatch(/couldn’t be loaded/);
    // A cached list whose refresh failed still holds the step, and so does a Retry in flight.
    expect(galleryProblem({ gallery: G, error: "down", loading: false })).toMatch(/press Retry/);
    expect(galleryProblem({ gallery: G, error: "down", loading: true })).toMatch(/press Retry/);
    // A cached list whose refresh is still pending: held, with a calm note rather than an error.
    expect(galleryProblem({ gallery: G, error: "", loading: true })).toBe("Checking the action list…");
    expect(galleryProblem({ gallery: G, error: "", loading: false })).toBeNull();
  });
});
