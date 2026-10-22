/** The words for the step a pending ask is on (#1171) — from the server's own progress events, so
 *  they say what is actually happening. Its own module for the fast-refresh rule: `AskWorking.tsx`
 *  exports a component and nothing else. */
export type AskStep =
  | { step: "catalog"; sessions: number; missions: number }
  | { step: "content"; candidates: number }
  // A mission turn's own steps (#1224): what it is deciding, and what an instruction reads.
  | { step: "classify" }
  | { step: "instruct"; sessions: number }
  // A durable Ask turn's own steps (#1443): the model reading, and the read tool it is calling.
  | { step: "thinking"; round: number }
  | { step: "tool"; tool: string };

/** What each read tool is doing, in the operator's words. An unknown tool reads as a lookup. */
const TOOL_WORDS: Record<string, string> = {
  find_sessions: "Searching your sessions and missions…",
  session_status: "Reading a session's status…",
  list_missions: "Reading your missions…",
  mission_status: "Reading a mission's status…",
  list_projects: "Listing your projects…",
  list_agents: "Listing the agents…",
  // #1554: the read-only usage, fleet and settings views.
  agent_usage: "Reading agent usage…",
  fleet_status: "Counting your sessions and missions…",
  read_settings: "Reading your settings…",
  // #1559: the session and project reads, and staging a change for the operator to confirm.
  list_sessions: "Listing your sessions…",
  session_brief: "Reading a session's brief…",
  list_folders: "Listing your folders…",
  propose_session_change: "Preparing the change for you to confirm…",
  propose_project_change: "Preparing the change for you to confirm…",
};

const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`;

export function askStepLabel(step: AskStep | null): string {
  if (!step || step.step === "classify") return "Reading your question…";
  if (step.step === "thinking")
    return step.round === 0 ? "Reading your question…" : "Putting the answer together…";
  if (step.step === "tool") return TOOL_WORDS[step.tool] ?? "Looking it up…";
  if (step.step === "instruct") {
    return step.sessions
      ? `Drafting the instruction for ${plural(step.sessions, "session", "sessions")}…`
      : "Drafting the instruction…";
  }
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
