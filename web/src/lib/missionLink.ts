/** The Missions section's route, and the one deep link into a specific mission (#948).
 *
 *  The PATHS moved to `routes.ts` in #1058, where every top-level route now lives together — the
 *  one-place rule this file stated first, applied to the whole set rather than to this section
 *  alone. They are re-exported here so every existing `from "…/missionLink"` import keeps working:
 *  one home for the values, two valid doors onto it.
 */
export { LEGACY_MISSION_PATH, MISSION_PATH } from "./routes";

import { MISSION_PATH } from "./routes";

/** The server's mission id shape (`missions.MISSION_ID_RE`). A deep link is shape-checked against
 *  it before it can select anything — the console's `?m=` and an outside link (#1232) alike. */
export const MISSION_ID_RE = /^msn_[0-9a-f]{32}$/;

/** A link that opens the Missions section with `missionId` selected. The console shape-checks the
 *  id before it can select anything, and settles the URL back on `MISSION_PATH`. */
export function missionLink(missionId: string): string {
  return `${MISSION_PATH}?m=${encodeURIComponent(missionId)}`;
}
