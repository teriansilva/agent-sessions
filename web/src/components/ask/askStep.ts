/** The words for the step a pending ask is on (#1171) — from the server's own progress events, so
 *  they say what is actually happening. Its own module for the fast-refresh rule: `AskWorking.tsx`
 *  exports a component and nothing else. */
export type AskStep =
  | { step: "catalog"; sessions: number; missions: number }
  | { step: "content"; candidates: number };

const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

export function askStepLabel(step: AskStep | null): string {
  if (!step) return "Reading your question…";
  if (step.step === "catalog") {
    const what = [
      step.sessions ? plural(step.sessions, "session", "sessions") : "",
      step.missions ? plural(step.missions, "mission", "missions") : "",
    ]
      .filter(Boolean)
      .join(" and ");
    return `Searching ${what || "your work"}…`;
  }
  return step.candidates
    ? `Checking against ${plural(step.candidates, "transcript", "transcripts")}…`
    : "Checking against the transcripts…";
}
