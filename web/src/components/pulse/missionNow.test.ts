/** Wording of the live strip (#1064): facts in words, and "not observed" never reads as quiet. */
import { expect, test } from "vitest";

import type { MissionNowSession } from "../../types/api";

import { ago, nowPhrase, recapPhrase, sessionLabel } from "./missionNow";

const row = (over: Partial<MissionNowSession>): MissionNowSession => ({
  session_key: "claude:3f2a0000-0000-4000-8000-000000000000",
  status: "producing",
  seconds_since_output: 4,
  prompt_class: null,
  recap_age_s: null,
  recap_older_than_output: false,
  ...over,
});

test.each([
  [0, "0s"],
  [59, "59s"],
  [60, "1m"],
  [3599, "59m"],
  [3660, "1h 1m"],
])("ago(%s) = %s", (s, want) => expect(ago(s)).toBe(want));

test.each<[Partial<MissionNowSession>, string]>([
  [
    { status: "producing", seconds_since_output: 4 },
    "producing output · last 4s ago",
  ],
  [
    { status: "at_prompt", prompt_class: "choice", seconds_since_output: 30 },
    "waiting at a choice · quiet 30s",
  ],
  [
    { status: "at_prompt", prompt_class: "confirm", seconds_since_output: 5 },
    "waiting at a confirmation · quiet 5s",
  ],
  [
    { status: "at_prompt", prompt_class: "question", seconds_since_output: 5 },
    "waiting at a question · quiet 5s",
  ],
  [{ status: "quiet", seconds_since_output: 125 }, "quiet for 2m"],
  [{ status: "unobserved", seconds_since_output: null }, "not observed yet"],
])("nowPhrase %j", (over, want) => expect(nowPhrase(row(over))).toBe(want));

test("a recap written before the latest output says so", () => {
  expect(recapPhrase(row({ recap_age_s: null }))).toBeNull();
  expect(recapPhrase(row({ recap_age_s: 180 }))).toBe("recap 3m ago");
  expect(
    recapPhrase(row({ recap_age_s: 180, recap_older_than_output: true })),
  ).toBe("recap 3m ago · written before the latest output");
});

test.each([
  ["claude:3f2a0000-0000-4000-8000-000000000000", "claude · 3f2a"],
  ["opencode:ses_ab12cd", "opencode · ab12"],
  ["kimi:session_9f8e7d", "kimi · 9f8e"],
])("sessionLabel(%s)", (key, want) => expect(sessionLabel(key)).toBe(want));
