/** The process row's pure half (#2076): building the step list from stored records and progress
 *  events, the times, the tallies, and what a stopped turn may say about sending. */
import { expect, test } from "vitest";

import type { AskToolStep, AskTurnEvent } from "../../types/api";
import {
  applyProcessEvent,
  formatElapsed,
  formatStepTime,
  moreLines,
  processFromServer,
  processTally,
  startedProcess,
  stepArgument,
  stepOrdinal,
  stepResult,
  stoppedLine,
} from "./askProcess";
import { askStepLabel, TOOL_LABELS, toolLabel } from "./askStep";

const step = (over: Partial<AskToolStep> = {}): AskToolStep => ({
  name: "session_status",
  outcome: "ok",
  ...over,
});

test("elapsed time reads `42 s`, `1 m 48 s`, then hours", () => {
  expect(formatElapsed(0)).toBe("0 s");
  expect(formatElapsed(42_900)).toBe("42 s");
  expect(formatElapsed(108_000)).toBe("1 m 48 s");
  expect(formatElapsed(842_000)).toBe("14 m 02 s");
  expect(formatElapsed(3_725_000)).toBe("1 h 02 m");
  expect(formatElapsed(-5)).toBe("0 s");
});

test("a step's own time has one decimal", () => {
  expect(formatStepTime(800)).toBe("0.8 s");
  expect(formatStepTime(2_449)).toBe("2.4 s");
  expect(formatStepTime(75_000)).toBe("1 m 15 s");
});

test("the list is built from the stored records, the totals from the server", () => {
  const tools = [step({ n: 1, ms: 800 }), step({ n: 2, ms: 300, name: "list_sessions" })];
  const p = processFromServer({ tools, steps: { count: 2, more: null, incomplete: false }, elapsed_ms: 48_000 }, 1_000);
  expect(p.steps).toEqual(tools);
  expect([p.count, p.more, p.incomplete, p.live]).toEqual([2, null, false, null]);
  expect([p.elapsedMs, p.elapsedAt]).toEqual([48_000, 1_000]);
  expect(processTally(p, false)).toBe("2 steps");
  // An older server sends no totals: the stored records are the count.
  expect(processFromServer({ tools }, 0).count).toBe(2);
  expect(processTally(processFromServer({ tools: [step()] }, 0), false)).toBe("1 step");
});

test("a running turn read back shows the server's live count, never the stored 200", () => {
  const tools = Array.from({ length: 200 }, (_, i) => step({ n: i + 1 }));
  const p = processFromServer(
    {
      tools,
      steps: {
        count: 1_440,
        more: { count: 1_240, ms: 9, tools: { session_status: 1_240 }, refused: {} },
        incomplete: false,
        live: { n: 1_441, tool: "list_sessions", at: 1_700_000_000 },
      },
      elapsed_ms: 842_000,
    },
    5_000,
  );
  expect(p.count).toBe(1_440);
  // The live step is timed on THIS page's clock, not the server's.
  expect(p.live).toEqual({ n: 1_441, tool: "list_sessions", at: 5_000 });
  expect(processTally(p, false)).toBe("1,440 steps");
});

test("progress lines build the running list: a step starts, ends, and the model thinks", () => {
  let p = startedProcess(100);
  const feed = (ev: AskTurnEvent, now: number) => (p = applyProcessEvent(p, ev, now));
  feed({ type: "progress", step: "thinking", round: 0, steps: 0 }, 110);
  expect(p.live).toBeNull();
  feed({ type: "progress", step: "tool", tool: "find_sessions", n: 1, at: 1.5 }, 120);
  expect(p.live).toEqual({ n: 1, tool: "find_sessions", at: 120 });
  // A search's own sub-steps change the words, never the list.
  feed({ type: "progress", step: "catalog", sessions: 4, missions: 0 }, 125);
  expect(p.live?.n).toBe(1);
  const record = step({ name: "find_sessions", n: 1, ms: 800, count: 12 });
  feed({ type: "progress", step: "tool_done", tool: "find_sessions", n: 1, ms: 800, outcome: "ok", steps: 1, more: 0, record }, 130);
  expect([p.steps, p.count, p.live]).toEqual([[record], 1, null]);
  // The same line twice (a reconnect) adds nothing.
  feed({ type: "progress", step: "tool_done", tool: "find_sessions", n: 1, ms: 800, outcome: "ok", steps: 1, more: 0, record }, 131);
  expect(p.steps).toHaveLength(1);
  // Past the stored steps a finished step has no record: only the totals move.
  feed({ type: "progress", step: "tool_done", tool: "session_status", n: 201, ms: 5, outcome: "ok", steps: 201, more: 1, record: null }, 140);
  expect([p.steps.length, p.count, p.more?.count]).toEqual([1, 201, 1]);
  feed({ type: "progress", step: "thinking", round: 2, steps: 201 }, 150);
  expect(p.live).toBeNull();
  expect(p.elapsedAt).toBe(100); // the clock started when the question was sent
});

test("a turn that ended without its summary says its count is incomplete, never `200 steps`", () => {
  const tools = Array.from({ length: 200 }, (_, i) => step({ n: i + 1 }));
  const p = processFromServer({ tools, steps: { count: 200, more: null, incomplete: true } }, 0);
  expect(processTally(p, false)).toBe("200+ steps, count incomplete");
  expect(processTally(p, true)).toBe("200+ steps, count incomplete");
});

test("a stopped turn's tally says so", () => {
  const p = processFromServer({ tools: Array.from({ length: 9 }, (_, i) => step({ n: i + 1 })) }, 0);
  expect(processTally(p, true)).toBe("Stopped after 9 steps");
  expect(processTally(processFromServer({ tools: [] }, 0), true)).toBe("Stopped after 0 steps");
});

test("`Nothing was sent` is said only when the stored send records confirm it", () => {
  const reads = [step({ n: 1 }), step({ n: 2, error: "unknown_key", outcome: "refused" })];
  expect(stoppedLine([])).toBe("Stopped by you. Nothing was sent.");
  expect(stoppedLine(reads)).toBe("Stopped by you. Nothing was sent.");
  // A send that was refused, or that settled without sending, sent nothing.
  expect(stoppedLine([...reads, step({ name: "send_nudge", sent: "no" })])).toBe(
    "Stopped by you. Nothing was sent.",
  );
  expect(stoppedLine([step({ name: "send_nudge", outcome: "refused", error: "pending" })])).toBe(
    "Stopped by you. Nothing was sent.",
  );
  // A delivered send: the sentence is left out — the send is a step in the list.
  expect(stoppedLine([...reads, step({ name: "send_nudge", sent: "yes", effect: true })])).toBe(
    "Stopped by you.",
  );
  // An outcome nobody knows is said as that, whatever else was delivered.
  const unknown = "Stopped by you. A message may have been sent — see the steps.";
  expect(stoppedLine([step({ name: "send_nudge", sent: "unknown", effect: true })])).toBe(unknown);
  expect(
    stoppedLine([
      step({ name: "send_nudge", sent: "yes" }),
      step({ name: "send_nudge", outcome: "refused", error: "uncertain", effect: true }),
    ]),
  ).toBe(unknown);
});

test("a step line says what it was about and what came back, from stored metadata only", () => {
  expect(stepOrdinal(step({ n: 7 }), 0)).toBe("07");
  expect(stepOrdinal(step({ n: 199 }), 0)).toBe("199");
  expect(stepOrdinal(step(), 4)).toBe("05"); // a record from before #2076: its place in the list
  expect(stepArgument(step({ name: "find_sessions", args: { query: "#1613" } }))).toBe("“#1613”");
  const titles = new Map([["claude:abc", "web: memory review panel"]]);
  expect(stepArgument(step({ args: { key: "claude:abc" } }), titles)).toBe("web: memory review panel");
  expect(stepArgument(step({ args: { key: "claude:zzz" } }), titles)).toBe("claude:zzz");
  expect(stepArgument(step())).toBe("");

  expect(stepResult(step({ count: 1 }))).toEqual({ text: "1 session", tone: "ok" });
  expect(stepResult(step({ name: "list_sessions", count: 38 }))).toEqual({ text: "38 sessions", tone: "ok" });
  expect(stepResult(step({ name: "something_new", count: 3 }))).toEqual({ text: "3 results", tone: "ok" });
  expect(stepResult(step({ name: "connector_read", length: 1_240 }))).toEqual({
    text: "1,240 characters",
    tone: "ok",
  });
  expect(stepResult(step({ outcome: "refused", error: "unknown_key" }))).toEqual({
    text: "refused: unknown key",
    tone: "refused",
  });
  expect(stepResult(step({ name: "send_nudge", sent: "unknown", count: 1 }))).toEqual({
    text: "outcome unknown",
    tone: "unknown",
  });
  expect(stepResult(step({ name: "send_nudge", sent: "yes", count: 1 })).text).toBe("sent");
  expect(stepResult(step({ name: "send_nudge", sent: "no", count: 1 }))).toEqual({
    text: "not sent",
    tone: "refused",
  });
});

test("the closing line counts the steps past the stored ones, by tool, refusals apart", () => {
  const lines = moreLines({
    count: 1_240,
    ms: 0,
    tools: { session_status: 812, list_sessions: 301, find_sessions: 127 },
    refused: {},
  });
  expect(lines.head).toBe("… and 1,240 more lookups");
  expect(lines.byTool).toBe(
    "Reading a session's status × 812 · Listing your sessions × 301 · Searching your sessions and missions × 127",
  );
  const refused = moreLines({ count: 413, ms: 0, tools: { list_agents: 1 }, refused: { send_nudge: 412 } });
  expect(refused.byTool).toBe("Sending a message refused × 412 · Listing the agents × 1");
  expect(moreLines({ count: 1, ms: 0, tools: { list_agents: 1 }, refused: {} }).head).toBe(
    "… and 1 more lookup",
  );
});

test("ONE label table: the running words are the list's label with an ellipsis", () => {
  for (const [tool, { label }] of Object.entries(TOOL_LABELS)) {
    expect(label).not.toMatch(/[….]$/);
    expect(toolLabel(tool)).toBe(label);
    expect(askStepLabel({ step: "tool", tool })).toBe(`${label}…`);
  }
  expect(toolLabel("something_new")).toBe("Looking it up");
});
