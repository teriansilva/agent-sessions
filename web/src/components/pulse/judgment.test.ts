/** How a judged objective reads (#1088): a judgment is never an observation, and nothing the server
 *  did not establish renders as met. */
import { expect, test } from "vitest";

import type { MissionObjective } from "../../types/api";

import {
  judgedStateWord,
  judgmentOf,
  probeLabel,
  sourceLabel,
} from "./judgment";

const T = "transcript:claude:5f3c0000-0000-0000-0000-0000000000a1";

function row(observed: Record<string, unknown> | null, over: Partial<MissionObjective> = {}) {
  return {
    key: "finding",
    title: "A finding is written down",
    probe: "supervisor_judged",
    gate: true,
    state: "met",
    met_at: 1,
    observed,
    source: "playbook",
    ...over,
  } as MissionObjective;
}

const verdict = (over: Record<string, unknown> = {}) => ({
  at: 100,
  detail: "the finding names a cause",
  value: true,
  judged: {
    met: true,
    confidence: 0.93,
    threshold: 0.9,
    evidence: [{ source: T, quote: "Root cause: the lock order." }],
    fingerprint: "f",
    checked_at: 100,
  },
  ...over,
});

test("a probe row is not a judged row", () => {
  expect(judgmentOf(row(null, { probe: "forge_pr" }))).toBeNull();
});

test("judged met at or above the threshold reads 'judged met (0.93)'", () => {
  const j = judgmentOf(row(verdict()))!;
  expect(j.kind).toBe("verdict");
  expect(judgedStateWord(j, "met")).toBe("judged met (0.93)");
});

test("below the threshold reads 'judged not yet' and does not count", () => {
  const j = judgmentOf(
    row(verdict({ value: false, judged: { ...verdict().judged, confidence: 0.62 } })),
  )!;
  expect(judgedStateWord(j, "met")).toBe("judged not yet (0.62)");
  expect(j.kind === "verdict" && j.counts).toBe(false);
});

test("a STALE judgment never counts, even when the stale record still says value", () => {
  const j = judgmentOf(row(verdict({ stale: true, reason: "changed", value: undefined })))!;
  expect(j.kind === "verdict" && j.stale && !j.counts).toBe(true);
});

test("an attempt with no verdict is UNKNOWN with the server's reason", () => {
  const j = judgmentOf(
    row(
      {
        at: 5,
        stale: true,
        reason: "cannot be judged — no AI endpoint is configured",
        judged: { attempted_fp: "f", transient: true },
      },
      { state: "pending" },
    ),
  )!;
  expect(j).toEqual({
    kind: "unknown",
    reason: "cannot be judged — no AI endpoint is configured",
    at: 5,
  });
});

test("a malformed record degrades to NOT JUDGED, never to met", () => {
  expect(judgmentOf(row({ judged: "nope", value: true }))).toEqual({ kind: "none" });
  const j = judgmentOf(row({ value: true, judged: { met: "yes", confidence: 1, threshold: 0.9 } }))!;
  expect(j.kind).toBe("unknown");
});

test("labels", () => {
  expect(sourceLabel(T)).toBe("transcript · claude:5f3c…a1");
  expect(sourceLabel("diff")).toBe("diff");
  expect(probeLabel("supervisor_judged")).toBe("Supervisor judges");
  expect(probeLabel("made_up")).toBe("made_up");
});

test("a MET row whose last attempt failed never reads 'met' (#1097 review)", () => {
  const j = judgmentOf(
    row({
      at: 5,
      stale: true,
      reason: "the judge could not answer: endpoint did not answer within 30s",
      judged: { attempted_fp: "f", transient: true },
      last: { value: true, confidence: 0.93, at: 1 },
    }),
  )!;
  const word = judgedStateWord(j, "met 14:02", "met");
  expect(word).toBe("judged earlier · not current");
  expect(word).not.toMatch(/\bmet\b/);
  // A pending row keeps its own word.
  expect(judgedStateWord(j, "pending", "pending")).toBe("pending");
});
