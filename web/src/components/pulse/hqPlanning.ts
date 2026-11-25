/** How a mission is handed to HQ (#2152). Its own module (no component), so the files that render
 *  components keep fast refresh. HQ plans every mission: there is no New-mission form. */
import { HQ_PATH } from "../../lib/routes";
import type { Mission } from "../../types/api";

/** Where every "New mission" goes: HQ's page on a FRESH conversation (the state `/ask` uses). HQ
 *  asks for the goal and the project itself, then proposes the draft-mission card. Nothing is
 *  created until the operator confirms that card. */
export const NEW_MISSION_TARGET = { to: HQ_PATH, state: { hqCompose: true } } as const;

/** The message Plan with HQ hands to HQ for an existing draft. The mission itself travels as the
 *  conversation's binding (`mission`), never as text HQ must parse. */
export function planThisMission(mission: Pick<Mission, "title">): string {
  return `Plan this mission: ${mission.title}`;
}
