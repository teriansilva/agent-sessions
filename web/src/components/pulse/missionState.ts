/** A mission's state as the operator reads it: one label and one dot colour, shared by the rail and
 *  the header's state chip (#967).
 *
 *  They were the rail's private helpers. The header grew a chip for the same fact, and a second copy
 *  is how "dispatching" would come to read "starting" in one place and "dispatching" in the other.
 *  Needs-you is not a state: the rail puts it ahead of these, and the header does not show it. */
import styles from "./mission.module.css";

export function missionStateLabel(state: string): string {
  return state === "dispatching" ? "starting" : state;
}

/** The state dot's class. SEMANTIC tokens only — the brand accent never means a state (§3).
 *
 *  Starting is NOT running (#967): green is the claim that work is underway, and a launch that is
 *  still in flight has not earned it. It is a neutral `--text-2` dot, as board A2 draws it. */
export function missionDotClass(state: string): string {
  if (state === "running") return styles.dotRunning;
  if (state === "dispatching") return styles.dotStarting;
  if (state === "failed") return styles.dotFailed;
  if (state === "done" || state === "abandoned") return styles.dotDone;
  return styles.dot;
}
