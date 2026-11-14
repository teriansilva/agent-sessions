import { expect, test } from "vitest";

import { askStepLabel } from "./askStep";

test("the read-only usage, fleet and settings tools say what they are doing (#1554)", () => {
  expect(askStepLabel({ step: "tool", tool: "agent_usage" })).toBe("Reading agent usage…");
  expect(askStepLabel({ step: "tool", tool: "fleet_status" })).toBe(
    "Counting your sessions and missions…",
  );
  expect(askStepLabel({ step: "tool", tool: "read_settings" })).toBe("Reading your settings…");
  // An unknown tool still reads as a lookup.
  expect(askStepLabel({ step: "tool", tool: "something_new" })).toBe("Looking it up…");
});

test("the search says what it searched: matching sessions, or only recent ones (#2028)", () => {
  // No keyword hit: the slice is recency padding, never "150 sessions".
  expect(askStepLabel({ step: "catalog", sessions: 150, missions: 0, hits: 0 })).toBe(
    "Searching your recent sessions…",
  );
  expect(askStepLabel({ step: "catalog", sessions: 150, missions: 4, hits: 3, missionHits: 0 })).toBe(
    "Searching 3 matching sessions and your recent ones…",
  );
  // Hits above the cap fill the slice: nothing was padded.
  expect(askStepLabel({ step: "catalog", sessions: 150, missions: 0, hits: 150 })).toBe(
    "Searching 150 matching sessions…",
  );
  expect(askStepLabel({ step: "catalog", sessions: 40, missions: 2, hits: 0, missionHits: 1 })).toBe(
    "Searching 1 matching mission and your recent ones…",
  );
  // An older server's event, without `hits`, keeps the earlier wording.
  expect(askStepLabel({ step: "catalog", sessions: 150, missions: 2 })).toBe(
    "Searching 150 sessions and 2 missions…",
  );
  expect(askStepLabel({ step: "classify" })).toBe("Reading your question…");
});
