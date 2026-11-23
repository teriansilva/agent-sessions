import { describe, expect, it } from "vitest";

import type { HqTask } from "../../../types/api";
import {
  canStopAll,
  countsLine,
  otherConversations,
  runningElapsed,
  taskAge,
  taskQuestion,
  taskRow,
  tasksPollMs,
  TASKS_POLL_ACTIVE_MS,
  TASKS_POLL_IDLE_MS,
  workingMark,
} from "./hqTasks";

const task = (over: Partial<HqTask>): HqTask => ({
  cid: "ask_" + "a".repeat(32),
  turn_id: "t1",
  state: "running",
  question: "How far are we with #1613?",
  via: null,
  conversation: "Where is #1613?",
  steps: 7,
  step: "session_status",
  stopping: false,
  asked_at: 100,
  started_at: 100,
  ended_at: null,
  elapsed_ms: 42_000,
  ...over,
});

const NOW = 1_000_000;

describe("the tasks strip's counts (#2103)", () => {
  it("words what is active and leaves out what is not", () => {
    expect(countsLine({ running: 1, queued: 2, question: 0 })).toBe("1 running · 2 queued");
    expect(countsLine({ running: 2, queued: 0, question: 1 })).toBe("2 running · 1 question");
    expect(countsLine({ running: 0, queued: 0, question: 2 })).toBe("2 questions");
    expect(countsLine({ running: 0, queued: 0, question: 0 })).toBe("");
  });

  it("offers Stop all only when more than one task exists", () => {
    expect(canStopAll({ running: 1, queued: 0, question: 0 })).toBe(false);
    expect(canStopAll({ running: 1, queued: 1, question: 0 })).toBe(true);
    expect(canStopAll({ running: 2, queued: 0, question: 0 })).toBe(true);
    // A parked question has nothing to stop.
    expect(canStopAll({ running: 1, queued: 0, question: 3 })).toBe(false);
  });

  it("reads closely while anything runs or waits", () => {
    expect(tasksPollMs({ running: 1, queued: 0, question: 0 })).toBe(TASKS_POLL_ACTIVE_MS);
    expect(tasksPollMs({ running: 0, queued: 1, question: 0 })).toBe(TASKS_POLL_ACTIVE_MS);
    expect(tasksPollMs({ running: 0, queued: 0, question: 1 })).toBe(TASKS_POLL_IDLE_MS);
  });
});

describe("the sidebar's strip leaves out its own conversation", () => {
  const A = "ask_" + "a".repeat(32);
  const B = "ask_" + "b".repeat(32);
  const list = {
    tasks: [
      task({ cid: A, turn_id: "a1", state: "running" }),
      task({ cid: A, turn_id: "a2", state: "queued" }),
      task({ cid: B, turn_id: "b1", state: "running" }),
      task({ cid: B, turn_id: "b2", state: "question" }),
    ],
    counts: { running: 2, queued: 1, question: 1 },
  };

  it("lists and counts only the other conversations' tasks", () => {
    const o = otherConversations(list, A);
    expect(o.tasks.map((t) => t.turn_id)).toEqual(["b1", "b2"]);
    expect(o.counts).toEqual({ running: 1, queued: 0, question: 1 });
    expect(o.hidesStoppable).toBe(true);
  });

  it("is empty when the open conversation is the only one working", () => {
    const o = otherConversations({ tasks: list.tasks.slice(0, 2), counts: list.counts }, A);
    expect(o.tasks).toEqual([]);
    expect(countsLine(o.counts)).toBe("");
  });

  it("hides nothing stoppable when the open conversation only has a question", () => {
    const o = otherConversations(list, B);
    expect(o.tasks.map((t) => t.turn_id)).toEqual(["a1", "a2"]);
    expect(otherConversations({ tasks: [list.tasks[3]], counts: list.counts }, B).hidesStoppable).toBe(false);
  });

  it("keeps the server's list and counts with no conversation open", () => {
    const o = otherConversations(list, null);
    expect(o.tasks).toBe(list.tasks);
    expect(o.counts).toBe(list.counts);
    expect(o.hidesStoppable).toBe(false);
  });
});

describe("the working mark (#2103)", () => {
  it("is hidden when nothing runs, queues or waits", () => {
    expect(workingMark({ running: 0, queued: 0, question: 0 })).toBeNull();
  });

  it("counts every task and names them in its label", () => {
    expect(workingMark({ running: 1, queued: 0, question: 0 })).toEqual({
      count: 1,
      question: false,
      label: "HQ tasks: 1 running",
    });
    expect(workingMark({ running: 1, queued: 2, question: 0 })).toEqual({
      count: 3,
      question: false,
      label: "HQ tasks: 1 running, 2 queued",
    });
  });

  it("says when a task is parked on a question", () => {
    expect(workingMark({ running: 2, queued: 0, question: 1 })).toEqual({
      count: 3,
      question: true,
      label: "HQ tasks: 2 running, 1 has a question",
    });
    expect(workingMark({ running: 0, queued: 0, question: 2 })?.label).toBe(
      "HQ tasks: 2 have a question",
    );
  });
});

describe("a task's row (#2103)", () => {
  it("shows a running task's step, tally and Stop", () => {
    expect(taskRow(task({}), NOW, 42_000)).toEqual({
      key: `ask_${"a".repeat(32)}:t1`,
      tone: "run",
      led: "",
      state: "Reading a session's status…",
      tally: "7 steps · 42 s",
      question: "How far are we with #1613?",
      conversation: "Where is #1613?",
      action: "stop",
    });
    expect(taskRow(task({ steps: 1, step: null }), NOW, null).tally).toBe("1 step");
    expect(taskRow(task({ stopping: true }), NOW, 0).state).toBe("Stopping…");
  });

  it("shows a queued task with Withdraw and no tally", () => {
    const row = taskRow(task({ state: "queued", steps: 0, step: null }), NOW, null);
    expect([row.tone, row.led, row.state, row.tally, row.action]).toEqual([
      "wait",
      "idle",
      "Queued",
      "",
      "withdraw",
    ]);
  });

  it("shows a parked task in the warn tone with Answer and no Stop", () => {
    const row = taskRow(task({ state: "question", ended_at: NOW / 1000 - 120 }), NOW, null);
    expect([row.tone, row.led, row.state, row.tally, row.action]).toEqual([
      "ask",
      "att",
      "HQ has a question",
      "2 m",
      "answer",
    ]);
  });

  it("words each settled outcome, with an age and Open", () => {
    const ended = NOW / 1000 - 31 * 60;
    const words = (state: HqTask["state"]) => {
      const row = taskRow(task({ state, ended_at: ended }), NOW, null);
      return [row.state, row.tone, row.tally, row.action];
    };
    expect(words("answered")).toEqual(["Answered", "rec", "31 m", "open"]);
    expect(words("stopped")).toEqual(["Stopped", "rec", "31 m", "open"]);
    expect(words("withdrawn")).toEqual(["Withdrawn", "rec", "31 m", "open"]);
    expect(words("failed")).toEqual(["Could not answer", "bad", "31 m", "open"]);
    expect(words("not_started")).toEqual([
      "Not started — the server restarted",
      "warn",
      "",
      "resend",
    ]);
  });

  it("never shows the server's own continuation note as a question", () => {
    expect(taskQuestion(task({ via: "card", question: "" }))).toBe("After your confirm");
    expect(taskQuestion(task({ question: "" }))).toBe("(no text)");
    expect(taskRow(task({ conversation: "" }), NOW, 0).conversation).toBe("Untitled conversation");
  });

  it("ages and ticks", () => {
    expect(taskAge(null, NOW)).toBe("");
    expect(taskAge(NOW / 1000 - 5, NOW)).toBe("now");
    expect(taskAge(NOW / 1000 - 4 * 60, NOW)).toBe("4 m");
    expect(taskAge(NOW / 1000 - 2 * 3600, NOW)).toBe("2 h");
    expect(runningElapsed(task({ elapsed_ms: 42_000 }), NOW, NOW + 3_000)).toBe(45_000);
    expect(runningElapsed(task({ elapsed_ms: null }), NOW, NOW)).toBeNull();
  });
});
