/** HQ's tasks as the strip and the working mark show them (#2103): pure derivations over the
 *  server's list (`GET /api/hq/tasks`). Everything here is the server's stored first lines,
 *  titles, counts and times; no model text reaches it. */
import type { HqTask, HqTaskList } from "../../../types/api";
import { formatElapsed } from "../../ask/askProcess";
import { askStepLabel } from "../../ask/askStep";

export const EMPTY_TASKS: HqTaskList = {
  tasks: [],
  recent: [],
  counts: { running: 0, queued: 0, question: 0 },
};

export type TaskCounts = HqTaskList["counts"];

const part = (n: number, word: string) => (n > 0 ? `${n} ${word}` : "");

/** `1 running · 2 queued`, `2 running · 1 question`; empty when nothing is active. */
export function countsLine(c: TaskCounts): string {
  return [
    part(c.running, "running"),
    part(c.queued, "queued"),
    part(c.question, c.question === 1 ? "question" : "questions"),
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The working mark: hidden when nothing runs, queues or waits; else the number of tasks, and
 *  whether one is parked on a question. */
export function workingMark(c: TaskCounts): { count: number; question: boolean; label: string } | null {
  const count = c.running + c.queued + c.question;
  if (count === 0) return null;
  const words = [
    part(c.running, "running"),
    part(c.queued, "queued"),
    c.question > 0
      ? `${c.question} ${c.question === 1 ? "has a question" : "have a question"}`
      : "",
  ].filter(Boolean);
  return { count, question: c.question > 0, label: `HQ tasks: ${words.join(", ")}` };
}

/** Stop all is offered only when more than one task could be stopped or withdrawn. */
export function canStopAll(c: TaskCounts): boolean {
  return c.running + c.queued > 1;
}

/** `4 m`, `31 m`, `2 h`, `now`: how long ago a task ended. */
export function taskAge(endedAt: number | null, nowMs: number): string {
  if (typeof endedAt !== "number") return "";
  const s = Math.max(0, Math.floor(nowMs / 1000 - endedAt));
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} m`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

export type TaskTone = "run" | "wait" | "ask" | "rec" | "bad" | "warn";

export interface TaskRowView {
  key: string;
  tone: TaskTone;
  /** The LED's state class (`hud-led`). */
  led: "" | "idle" | "att";
  /** The state line: the running step in words, `Queued`, `HQ has a question`, `Answered`… */
  state: string;
  /** Right of the state line: `7 steps · 42 s` while running, an age once settled. */
  tally: string;
  question: string;
  conversation: string;
  action: "stop" | "withdraw" | "answer" | "resend" | "open";
}

const RECENT_WORDS: Record<string, string> = {
  answered: "Answered",
  question: "HQ has a question",
  stopped: "Stopped",
  failed: "Could not answer",
  withdrawn: "Withdrawn",
  not_started: "Not started — the server restarted",
};

/** What a task's question line shows: its stored first line; the server's own continuation
 *  after a card has no question of the Commander's. */
export function taskQuestion(t: HqTask): string {
  if (t.via === "card") return "After your confirm";
  return t.question || "(no text)";
}

/** One row of the strip. `elapsedMs` is the running turn's time NOW (the caller ticks it). */
export function taskRow(t: HqTask, nowMs: number, elapsedMs: number | null): TaskRowView {
  const base = {
    key: `${t.cid}:${t.turn_id}`,
    question: taskQuestion(t),
    conversation: t.conversation || "Untitled conversation",
  };
  if (t.state === "running") {
    const steps = `${t.steps.toLocaleString("en-US")} ${t.steps === 1 ? "step" : "steps"}`;
    return {
      ...base,
      tone: "run",
      led: "",
      state: t.stopping
        ? "Stopping…"
        : askStepLabel(t.step ? { step: "tool", tool: t.step } : null),
      tally: elapsedMs === null ? steps : `${steps} · ${formatElapsed(elapsedMs)}`,
      action: "stop",
    };
  }
  if (t.state === "queued")
    return { ...base, tone: "wait", led: "idle", state: "Queued", tally: "", action: "withdraw" };
  if (t.state === "question")
    return {
      ...base,
      tone: "ask",
      led: "att",
      state: "HQ has a question",
      tally: taskAge(t.ended_at, nowMs),
      action: "answer",
    };
  const tone: TaskTone =
    t.state === "failed" ? "bad" : t.state === "not_started" ? "warn" : "rec";
  return {
    ...base,
    tone,
    led: "idle",
    state: RECENT_WORDS[t.state] ?? "Ended",
    tally: t.state === "not_started" ? "" : taskAge(t.ended_at, nowMs),
    action: t.state === "not_started" ? "resend" : "open",
  };
}

/** The running turn's time at `nowMs`: the server's figure plus what passed on this page since
 *  the list was read (`readAtMs`). */
export function runningElapsed(t: HqTask, readAtMs: number, nowMs: number): number | null {
  if (typeof t.elapsed_ms !== "number") return null;
  return t.elapsed_ms + Math.max(0, nowMs - readAtMs);
}

/** How often the list is read: closely while anything runs or waits, slowly otherwise. */
export const TASKS_POLL_ACTIVE_MS = 4_000;
export const TASKS_POLL_IDLE_MS = 30_000;

export function tasksPollMs(c: TaskCounts): number {
  return c.running + c.queued > 0 ? TASKS_POLL_ACTIVE_MS : TASKS_POLL_IDLE_MS;
}
