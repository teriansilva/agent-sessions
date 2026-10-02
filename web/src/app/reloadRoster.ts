import { api } from "../lib/api";
import { setRoster } from "./engineRoster";

/** Re-read `/api/engines` now — after a change that alters an engine's availability, such as
 *  saving an API agent's endpoint (#1209). The provider loads the roster once at app start; this
 *  is the explicit refresh. A failed read keeps the last good roster. */
export async function reloadRoster(): Promise<void> {
  try {
    const d = await api.engines();
    if (Array.isArray(d?.engines)) setRoster(d.engines, Array.isArray(d.problems) ? d.problems : []);
  } catch {
    /* keep the last good roster */
  }
}
