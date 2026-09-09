/** Where the mission rail renders, decided by the app shell (#935).
 *
 *  The console portals its rail into the shell's sidebar on the mission route, so it needs the
 *  destination element. It could look that element up by id in an effect — and the first version
 *  did — but that means `setState` synchronously inside an effect, which is a cascading render
 *  and which the `react-hooks` lint rejects on principle.
 *
 *  Context is the honest shape anyway: the shell is the component that DECIDES whether a slot is
 *  offered (it is not, where its sidebar is an off-canvas drawer without a focus trap), so the
 *  shell should say so rather than leave the console to infer it from the DOM. `null` means
 *  "render in place" — the fallback that keeps the console working standalone, in tests, and on
 *  any route that does not offer a slot.
 */
import { createContext, useContext } from "react";

const MissionRailSlot = createContext<HTMLElement | null>(null);

export const MissionRailSlotProvider = MissionRailSlot.Provider;

/** The element the mission rail should portal into, or `null` to render it in place. */
export function useMissionRailSlot(): HTMLElement | null {
  return useContext(MissionRailSlot);
}
