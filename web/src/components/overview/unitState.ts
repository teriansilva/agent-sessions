/** The unit states of the v5 map board (#1600), and the words a unit card and the legend show for
 *  them. Precedence, highest first: archived, then an intervention (AWAITING ORDERS — the
 *  operator holds the next move), then a working agent (ENGAGED), then a live agent process that
 *  is quiet (STANDBY), else OFFLINE. The board's FAILED has no source among the session's fields
 *  (a session records no failure state), so the map shows OFFLINE where no agent runs instead.
 *  The LED takes its colour from the state's semantic token, never from the brand amber. */
export type UnitState = "archived" | "awaiting" | "engaged" | "standby" | "offline";
export const STATE_WORD: Record<UnitState, string> = {
  archived: "Archived",
  awaiting: "Awaiting orders",
  engaged: "Engaged",
  standby: "Standby",
  offline: "Offline",
};
export function unitState(
  archived: boolean,
  intervention: boolean,
  working: boolean,
  live: boolean,
): UnitState {
  if (archived) return "archived";
  if (intervention) return "awaiting";
  if (working) return "engaged";
  return live ? "standby" : "offline";
}
