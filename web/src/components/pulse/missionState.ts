/** A mission's state as the operator reads it: one label and one dot colour, shared by the rail and
 *  the header's state chip (#967).
 *
 *  They were the rail's private helpers. The header grew a chip for the same fact, and a second copy
 *  is how "dispatching" would come to read "starting" in one place and "dispatching" in the other.
 *  Needs-you is not a state: the rail puts it ahead of these, and the header does not show it. */
import type { LedTone } from "../hud/Meters";
import styles from "./mission.module.css";

export function missionStateLabel(state: string): string {
  return state === "dispatching" ? "starting" : state;
}

/** The state dot's class. SEMANTIC tokens only — the brand accent never means a state (§3).
 *
 *  Starting is NOT running (#967): green is the claim that work is underway, and a launch that is
 *  still in flight has not earned it. It is a neutral `--text-2` dot, as board A2 draws it. */
export function missionDotClass(state: string): string {
  const { tone, shape } = missionLed(state);
  if (shape === "starting") return styles.dotStarting;
  if (shape === "done") return styles.dotDone;
  if (tone === "up") return styles.dotRunning;
  if (tone === "down") return styles.dotFailed;
  if (tone === "draft") return styles.dotDraft;
  return styles.dot;
}

/** THE ONE MAPPING from a mission's state to its light (#1573), for every surface that draws one:
 *  the rail's `Led`, the header's state chip and the thread's state chips. `tone` is the LED tone;
 *  `shape` marks the two that are not a plain light — starting (a neutral solid, never green: a
 *  launch in flight has not earned "running") and done / abandoned (a hollow ring: done is not a
 *  light). Needs-you outranks the state wherever a surface shows it (the rail); the header does not. */
export type MissionLedShape = "starting" | "done" | null;
export function missionLed(
  state: string,
  needsYou = false,
): { tone: LedTone; shape: MissionLedShape } {
  if (needsYou) return { tone: "att", shape: null };
  switch (state) {
    case "running":
      return { tone: "up", shape: null };
    case "failed":
      return { tone: "down", shape: null };
    // A draft is the session rows' draft (#477): the shared `--status-draft` tone.
    case "draft":
      return { tone: "draft", shape: null };
    case "dispatching":
      return { tone: "idle", shape: "starting" };
    case "done":
    case "abandoned":
      return { tone: "idle", shape: "done" };
    default:
      return { tone: "idle", shape: null };
  }
}
