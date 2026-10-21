import { api } from "../lib/api";
import { setRoster } from "./engineRoster";

/** What a reload achieved: a roster installed, or nothing (the last good roster is kept). */
export type RosterReload = "ok" | "failed";

/** Re-read `/api/engines` now — after a change that alters an engine's availability, such as
 *  saving an API agent's endpoint (#1209), or when a terminal socket was refused as "not a
 *  terminal session" (#1502). The provider loads the roster once at app start; this is the
 *  explicit refresh. A failed read keeps the last good roster and resolves `"failed"`, so a
 *  waiting state can say so instead of waiting on a reload that will never land. Never rejects. */
export async function reloadRoster(): Promise<RosterReload> {
  try {
    const d = await api.engines();
    if (!Array.isArray(d?.engines)) return "failed";
    setRoster(d.engines, Array.isArray(d.problems) ? d.problems : []);
    return "ok";
  } catch {
    return "failed";
  }
}
