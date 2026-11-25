/** The objective list's order and reason rules (#967 P3), without React or the drag library. */
import { describe, expect, test } from "vitest";

import type { MissionSupervisor, SupervisorObjective } from "../../types/api";

import {
  clampOverflows,
  inOrder,
  moveKey,
  sharedReason,
} from "./objectiveOrder";

const NO_SESSION = "this mission holds no session, so there is nothing to nudge";
const UNREADABLE =
  "the mission's sessions could not be read, so nothing may be sent";
const SPENT = "the 3-nudge budget for this episode is spent";

describe("moveKey — the one computation of a new order", () => {
  test("dragging row 3 above row 1 gives the full list in the new order", () => {
    expect(moveKey(["a", "b", "c", "d"], 2, 0)).toEqual(["c", "a", "b", "d"]);
  });

  test("moving down shifts the rows in between up, it does not swap the ends", () => {
    expect(moveKey(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
  });

  test("Move up / Move down by one is the same op as a one-row drag", () => {
    expect(moveKey(["a", "b", "c"], 1, 0)).toEqual(["b", "a", "c"]);
    expect(moveKey(["a", "b", "c"], 1, 2)).toEqual(["a", "c", "b"]);
  });

  test("every key is still there exactly once", () => {
    const out = moveKey(["a", "b", "c", "d", "e"], 4, 1);
    expect([...out].sort()).toEqual(["a", "b", "c", "d", "e"]);
    expect(out).toHaveLength(5);
  });

  test("a no-op or out-of-range move returns an unchanged COPY", () => {
    const keys = ["a", "b"];
    for (const [from, to] of [
      [0, 0],
      [-1, 0],
      [0, 2],
      [2, 0],
    ]) {
      const out = moveKey(keys, from, to);
      expect(out).toEqual(["a", "b"]);
      expect(out).not.toBe(keys);
    }
  });
});

describe("inOrder — a pending order is only shown over the list it was made for", () => {
  const rows = [{ key: "a" }, { key: "b" }, { key: "c" }];

  test("lays the rows out in the pending order", () => {
    expect(inOrder(rows, ["c", "a", "b"]).map((r) => r.key)).toEqual([
      "c",
      "a",
      "b",
    ]);
  });

  test("no pending order is the server's order", () => {
    expect(inOrder(rows, null).map((r) => r.key)).toEqual(["a", "b", "c"]);
  });

  test("a list that changed underneath falls back to the server's order", () => {
    // Added, dropped, renamed-key and duplicated: none of these is the list the drop was made on.
    for (const keys of [
      ["c", "a", "b", "d"],
      ["c", "a"],
      ["c", "a", "x"],
      ["a", "a", "b"],
    ]) {
      expect(inOrder(rows, keys).map((r) => r.key)).toEqual(["a", "b", "c"]);
    }
  });
});

function reading(over: Partial<SupervisorObjective>): SupervisorObjective {
  return {
    key: "k",
    title: "t",
    gate: false,
    state: "open",
    met: false,
    episode: 1,
    stood_down: false,
    awaiting_answer: false,
    spent: 0,
    remaining: 3,
    may_nudge: false,
    unreadable: false,
    indeterminate: false,
    live: 0,
    terminal: false,
    why_not: "",
    ...over,
  };
}

function sup(
  objectives: SupervisorObjective[],
  over: Partial<MissionSupervisor> = {},
): MissionSupervisor {
  return {
    objectives,
    likely_done: false,
    unmet_gates: 0,
    checked_at: 1,
    ...over,
  };
}

describe("sharedReason — the no-session sentence is said once", () => {
  test("no session: every row's sentence is the same, so it is said once", () => {
    const s = sup(
      [
        reading({ key: "a", why_not: NO_SESSION }),
        reading({ key: "b", why_not: NO_SESSION }),
        reading({ key: "c", why_not: NO_SESSION }),
      ],
      { no_session: true },
    );
    expect(sharedReason(s)).toBe(NO_SESSION);
  });

  test("a row whose reason differs is not what gets lifted", () => {
    const s = sup(
      [
        reading({ key: "a", why_not: SPENT }),
        reading({ key: "b", why_not: NO_SESSION }),
        reading({ key: "c", why_not: NO_SESSION }),
      ],
      { no_session: true },
    );
    expect(sharedReason(s)).toBe(NO_SESSION);
  });

  test("an unreadable roster is the other mission-level refusal", () => {
    const s = sup([reading({ why_not: UNREADABLE })], {
      sessions_unreadable: true,
    });
    expect(sharedReason(s)).toBe(UNREADABLE);
  });

  test("a mission holding a session keeps every reason on its own row", () => {
    // Identical sentences are not enough: two spent budgets are two facts about two objectives.
    const s = sup(
      [reading({ key: "a", why_not: SPENT }), reading({ key: "b", why_not: SPENT })],
      { no_session: false },
    );
    expect(sharedReason(s)).toBeNull();
  });

  test("no assessment, or no sentence at all, shares nothing", () => {
    expect(sharedReason(undefined)).toBeNull();
    expect(
      sharedReason(sup([reading({ why_not: "" })], { no_session: true })),
    ).toBeNull();
  });
});

describe("clampOverflows — a title is a control only when the clamp hides something", () => {
  test("content taller than the clamped box is clipped", () => {
    expect(clampOverflows(88, 35)).toBe(true);
  });
  test("content that fits, or differs by a rounding pixel, is not", () => {
    expect(clampOverflows(35, 35)).toBe(false);
    expect(clampOverflows(36, 35)).toBe(false);
  });
  test("a box with no height yet (a closed disclosure) hides nothing", () => {
    expect(clampOverflows(88, 0)).toBe(false);
  });
});
