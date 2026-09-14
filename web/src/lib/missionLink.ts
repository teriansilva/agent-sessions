/** The Missions section's route, and the one deep link into a specific mission (#948).
 *
 *  ONE place, so the next rename is a one-line change. The `/pulse` → `/mission` rename touched 41
 *  files because the path was a literal everywhere a link or route was built; everything that
 *  links into the section imports it from here instead. `/pulse` stays only as the legacy redirect
 *  in `App.tsx`, which is the one place that must name the OLD path. */
export const MISSION_PATH = "/mission";

/** The pre-#948 path, still served as a permanent replace-redirect. */
export const LEGACY_MISSION_PATH = "/pulse";

/** A link that opens the Missions section with `missionId` selected. The console shape-checks the
 *  id before it can select anything, and settles the URL back on `MISSION_PATH`. */
export function missionLink(missionId: string): string {
  return `${MISSION_PATH}?m=${encodeURIComponent(missionId)}`;
}
