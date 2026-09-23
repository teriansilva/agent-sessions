/** The launch confirmation's words about the set (#1061 Phase 3): the issue's predicate, verbatim. */
import { expect, test } from "vitest";

import type { MissionObjective } from "../../types/api";

import { checksWhat, worthALook } from "./confirmSet";

const o = (over: Partial<MissionObjective>): MissionObjective =>
  ({
    mission_id: "m",
    key: "k",
    ord: 0,
    title: "t",
    probe: "forge_merged",
    probe_args: null,
    gate: true,
    state: "unmet",
    met_at: null,
    observed: null,
    source: "playbook",
    ...over,
  }) as MissionObjective;

test("an ordinary set says nothing", () => {
  expect(worthALook({ dropped: 0, parameterised: 0 }, [o({})])).toEqual([]);
  expect(worthALook(undefined, [o({})])).toEqual([]);
});

test("each clause of the predicate is said on its own", () => {
  expect(worthALook({ dropped: 0, parameterised: 2 }, [o({})])).toEqual([
    "When this checklist was proposed, 2 objectives were fitted to targets named in your instruction — check they are the right ones.",
  ]);
  expect(worthALook({ dropped: 1, parameterised: 0 }, [o({})])).toEqual([
    "When it was proposed, 1 suggestion did not fit the checklist and was dropped.",
  ]);
  expect(
    worthALook({ dropped: 0, parameterised: 0 }, [o({ gate: false })]),
  ).toEqual([
    "Nothing here is required, so the mission can never confirm itself finished — you close it.",
  ]);
});

test("all three together, in order", () => {
  expect(
    worthALook({ dropped: 3, parameterised: 1 }, [o({ gate: false })]),
  ).toHaveLength(3);
});

test("an empty set is not 'nothing required' — Begin refuses it elsewhere", () => {
  expect(worthALook({ dropped: 0, parameterised: 0 }, [])).toEqual([]);
});

test.each<[Partial<MissionObjective>, string]>([
  [
    { probe: "forge_merged", probe_args: { branch: "devopsagent/alpha" } },
    "forge_merged · devopsagent/alpha",
  ],
  [
    { probe: "forge_checks", probe_args: { repo: "o/r", branch: "main" } },
    "forge_checks · o/r · main",
  ],
  [
    { probe: "http_status", probe_args: { url: "https://x/healthz" } },
    "http_status · https://x/healthz",
  ],
  [{ probe: "forge_merged", probe_args: null }, "forge_merged"],
  [{ probe: "none", gate: false }, "note · not checked"],
])("checksWhat %j", (over, want) => expect(checksWhat(o(over))).toBe(want));
