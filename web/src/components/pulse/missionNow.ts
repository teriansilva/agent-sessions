/** Wording for the live strip (#1064). Pure, so each branch is a table test.
 *
 *  The strip states FACTS the server derived — a status word, seconds since visible output, the
 *  prompt class while waiting, the recap's age. It never guesses at intent: "quiet" is not "done",
 *  and "not observed" is not "quiet" (the server has seen no output, so it cannot say). */
import type { MissionNowSession } from "../../types/api";

/** Missions whose sessions can be doing something. Everything else shows no strip. */
export const LIVE_STATES: ReadonlySet<string> = new Set([
  "dispatching",
  "running",
  "review",
]);

/** Poll cadence while the tab is visible. The route is cheap (no model call, no write). */
export const NOW_POLL_MS = 10_000;

export function ago(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const PROMPT_WORD: Record<string, string> = {
  choice: "a choice",
  confirm: "a confirmation",
  question: "a question",
};

/** The session's line: what it is doing, in words. */
export function nowPhrase(s: MissionNowSession): string {
  const since = s.seconds_since_output;
  switch (s.status) {
    case "producing":
      return since == null
        ? "producing output"
        : `producing output · last ${ago(since)} ago`;
    case "at_prompt":
      return (
        `waiting at ${PROMPT_WORD[s.prompt_class ?? ""] ?? "a prompt"}` +
        (since == null ? "" : ` · quiet ${ago(since)}`)
      );
    case "quiet":
      return since == null ? "quiet" : `quiet for ${ago(since)}`;
    default:
      return "not observed yet";
  }
}

/** The recap half of the line, or null when there has been no recap. */
export function recapPhrase(s: MissionNowSession): string | null {
  if (s.recap_age_s == null) return null;
  return s.recap_older_than_output
    ? `recap ${ago(s.recap_age_s)} ago · written before the latest output`
    : `recap ${ago(s.recap_age_s)} ago`;
}

/** `claude:3f2a…` → `claude · 3f2a`. The strip names sessions by engine and a short id. */
export function sessionLabel(key: string): string {
  const [engine, id] = key.includes(":") ? key.split(/:(.*)/s, 2) : ["", key];
  const short = (id ?? "").replace(/^(ses_|session_)/, "").slice(0, 4);
  return engine ? `${engine} · ${short}` : short;
}
