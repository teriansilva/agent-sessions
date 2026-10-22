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
