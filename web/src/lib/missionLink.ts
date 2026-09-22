/** The Missions section's route, and the one deep link into a specific mission (#948).
 *
 *  The PATHS moved to `routes.ts` in #1058, where every top-level route now lives together — the
 *  one-place rule this file stated first, applied to the whole set rather than to this section
 *  alone. They are re-exported here so every existing `from "…/missionLink"` import keeps working:
 *  one home for the values, two valid doors onto it.
 */
export { LEGACY_MISSION_PATH, MISSION_PATH } from "./routes";

import { MISSION_PATH } from "./routes";

/** A link that opens the Missions section with `missionId` selected. The console shape-checks the
 *  id before it can select anything, and settles the URL back on `MISSION_PATH`. */
export function missionLink(missionId: string): string {
  return `${MISSION_PATH}?m=${encodeURIComponent(missionId)}`;
}
