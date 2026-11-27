/** What an HQ turn did, as the process row shows it (#2076) — the pure half of `AskProcess.tsx`.
 *
 *  Everything here is built from the turn's stored tool records and the server's progress events:
 *  a tool's name, its guarded string arguments, counts, an error code, an ordinal, a duration.
 *  No model text and no tool output ever reaches it — none is stored. */
import type { AskSteps, AskStepsMore, AskToolStep, AskTurnEvent } from "../../types/api";
import { TOOL_LABELS, toolLabel } from "./askStep";

/** A turn's process: its stored steps, the totals, and (running) the step in flight. */
export interface AskProcessState {
  steps: AskToolStep[];
  /** Steps finished: the stored ones and the counted ones. */
  count: number;
  more: AskStepsMore | null;
  /** The turn ended without its summary: at least `steps.length` steps, how many is not known. */
  incomplete: boolean;
  /** The step running now. `at` is this page's clock when it learnt of it. */
  live: { n: number; tool: string; at: number } | null;
  /** How long the turn had run at `elapsedAt` (this page's clock), in milliseconds. */
  elapsedMs: number | null;
  elapsedAt: number;
}

export const EMPTY_PROCESS: AskProcessState = {
  steps: [],
  count: 0,
  more: null,
  incomplete: false,
  live: null,
  elapsedMs: null,
  elapsedAt: 0,
};

/** A running turn this page has just sent: its clock starts now. */
export function startedProcess(now: number): AskProcessState {
  return { ...EMPTY_PROCESS, elapsedMs: 0, elapsedAt: now };
}

/** The process of a turn as the server returned it. A running turn's totals are the SERVER's
 *  (`steps`), never a count of the stored records: past the stored steps they differ. */
export function processFromServer(
  t: { tools?: AskToolStep[]; steps?: AskSteps; elapsed_ms?: number | null },
  now: number,
): AskProcessState {
  const steps = t.tools ?? [];
  const live = t.steps?.live ?? null;
  return {
    steps,
    count: t.steps?.count ?? steps.length,
    more: t.steps?.more ?? null,
    incomplete: t.steps?.incomplete ?? false,
    // The server's start time is on ITS clock; the row only needs "since this page knew".
    live: live ? { n: live.n, tool: live.tool, at: now } : null,
    elapsedMs: typeof t.elapsed_ms === "number" ? t.elapsed_ms : null,
    elapsedAt: now,
  };
}

/** Fold one streamed progress line into the process. */
export function applyProcessEvent(
  p: AskProcessState,
  ev: AskTurnEvent,
  now: number,
): AskProcessState {
  if (ev.type !== "progress") return p;
  if (ev.step === "tool") {
    const n = typeof ev.n === "number" ? ev.n : p.count + 1;
    return { ...p, live: { n, tool: ev.tool, at: now } };
  }
  if (ev.step === "tool_done") {
    const known = ev.record !== null && p.steps.some((s) => s.n === ev.record?.n);
    const steps = ev.record && !known ? [...p.steps, ev.record] : p.steps;
    // Past the stored steps only the totals move: the per-tool counts come with the settled
    // turn, the number shows now.
    const more =
      ev.more > 0
        ? { ...(p.more ?? { ms: 0, tools: {}, refused: {} }), count: ev.more }
        : p.more;
    return { ...p, steps, count: Math.max(p.count, ev.steps), more, live: null };
  }
  // The model is reading between tools: no step is live.
  if (ev.step === "thinking")
    return { ...p, live: null, count: Math.max(p.count, ev.steps ?? 0) };
  return p;
}

/** `42 s`, `1 m 48 s`, `1 h 02 m`: how long a turn ran. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total} s`;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (total < 3600) return `${Math.floor(total / 60)} m ${pad(total % 60)} s`;
  return `${Math.floor(total / 3600)} h ${pad(Math.floor((total % 3600) / 60))} m`;
}

/** `0.8 s`: how long one step took. A long one reads as a turn's time does. */
export function formatStepTime(ms: number): string {
  if (ms < 60_000) return `${(Math.max(0, ms) / 1000).toFixed(1)} s`;
  return formatElapsed(ms);
}

const count = (n: number) => n.toLocaleString("en-US");
const steps = (n: number) => `${count(n)} ${n === 1 ? "step" : "steps"}`;

/** The settled row's words: `23 steps`, `Stopped after 9 steps`, or — for a turn that ended
 *  without its summary — `200+ steps, count incomplete` (never `200 steps`). */
export function processTally(p: AskProcessState, stopped: boolean): string {
  if (p.incomplete) return `${count(p.steps.length)}+ steps, count incomplete`;
  return stopped ? `Stopped after ${steps(p.count)}` : steps(p.count);
}

/** A two-digit ordinal; a step from before #2076 has none and takes its place in the list. */
export function stepOrdinal(step: AskToolStep, index: number): string {
  return String(step.n ?? index + 1).padStart(2, "0");
}

/** What the step was about: its first stored argument. A session key or a mission id reads as
 *  that session's or mission's title when this conversation knows it (`titles`). */
export function stepArgument(step: AskToolStep, titles?: ReadonlyMap<string, string>): string {
  const args = step.args ?? {};
  const value = args.query ?? args.key ?? args.mission_id ?? Object.values(args)[0] ?? "";
  if (!value) return "";
  if ("query" in args) return `“${value}”`;
  return titles?.get(value) ?? value;
}

export type StepTone = "ok" | "refused" | "unknown";

/** What came back, from the stored metadata alone: a count with the tool's own noun, a length,
 *  a send's outcome, or the refusal's code in words. */
export function stepResult(step: AskToolStep): { text: string; tone: StepTone } {
  if (step.sent === "unknown" || step.error === "uncertain")
    return { text: "outcome unknown", tone: "unknown" };
  if (step.error) return { text: `refused: ${step.error.replace(/_/g, " ")}`, tone: "refused" };
  if (step.sent === "yes") return { text: "sent", tone: "ok" };
  if (step.sent === "no") return { text: "not sent", tone: "refused" };
  // A command HQ ran itself (#2404): its exit code, the one fact that says how it went.
  if (typeof step.exit === "number")
    return { text: `exit ${step.exit}`, tone: step.exit === 0 ? "ok" : "refused" };
  if (typeof step.count === "number") {
    const [one, many] = TOOL_LABELS[step.name]?.noun ?? ["result", "results"];
    return { text: `${count(step.count)} ${step.count === 1 ? one : many}`, tone: "ok" };
  }
  if (typeof step.length === "number")
    return { text: `${count(step.length)} characters`, tone: "ok" };
  return { text: "", tone: "ok" };
}

/** The line under a stopped turn. "Nothing was sent" is said ONLY when the stored send records
 *  confirm it; with a delivered send the sentence is left out (the send is a step in the list);
 *  with a send whose outcome is not known, that is what it says. */
export function stoppedLine(stepsDone: readonly AskToolStep[]): string {
  const unknown = stepsDone.some(
    (s) => s.sent === "unknown" || (s.error === "uncertain" && s.name === "send_nudge"),
  );
  if (unknown) return "Stopped by you. A message may have been sent — see the steps.";
  if (stepsDone.some((s) => s.sent === "yes")) return "Stopped by you.";
  return "Stopped by you. Nothing was sent.";
}

/** The closing line for the steps past the stored ones: `… and 1,240 more lookups`, and what
 *  they were, most frequent first, refusals apart. */
export function moreLines(more: AskStepsMore): { head: string; byTool: string } {
  const rows = [
    ...Object.entries(more.tools).map(([name, n]) => ({ text: toolLabel(name), n })),
    ...Object.entries(more.refused).map(([name, n]) => ({
      text: `${toolLabel(name)} refused`,
      n,
    })),
  ].sort((x, y) => y.n - x.n || x.text.localeCompare(y.text));
  return {
    head: `… and ${count(more.count)} more ${more.count === 1 ? "lookup" : "lookups"}`,
    byTool: rows.map((r) => `${r.text} × ${count(r.n)}`).join(" · "),
  };
}
