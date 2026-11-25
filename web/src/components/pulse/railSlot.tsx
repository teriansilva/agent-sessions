/** Where the mission rail renders, decided by the app shell (#935, #940).
 *
 *  The console portals its rail into the shell's sidebar on the mission route, so it needs the
 *  destination element. It could look that element up by id in an effect — and the first version
 *  did — but that means `setState` synchronously inside an effect, which is a cascading render
 *  and which the `react-hooks` lint rejects on principle.
 *
 *  Context is the honest shape anyway: the shell is the component that DECIDES whether a slot is
 *  offered, so the shell should say so rather than leave the console to infer it from the DOM.
 *  `null` means "render in place" — the fallback that keeps the console working standalone, in
 *  tests, and on any route that does not offer a slot.
 *
 *  **`dismiss` is not optional decoration (#940).** Where the slot is a drawer, choosing a mission
 *  has to close it — and the console cannot do that itself: selection changes the console's local
 *  state, never the URL, so the shell's pathname effect never fires and the drawer would sit open
 *  over the mission the operator just picked. The shell owns the drawer, so the shell supplies the
 *  way to close it; the console calls it and stays ignorant of whether a drawer exists at all.
 */
import { createContext, useContext } from "react";

export interface MissionRailSlot {
  /** The element the mission rail should portal into, or `null` to render it in place. */
  el: HTMLElement | null;
  /** The shell's sidebar HEAD row (#948 P2): the rail's counts render here, in the same 38px row
   *  the sessions sidebar uses for its ORDER control. `null` renders them in place. */
  headEl: HTMLElement | null;
  /** The shell's sidebar FOOTER (#948 §1): mission and held-session telemetry render here, where
   *  the sessions sidebar shows ENGAGED · LIVE. `null` renders nothing — the shell owns a footer. */
  footEl: HTMLElement | null;
  /** Close the surface the slot lives in, if it is one that closes. A no-op for a docked column,
   *  so the console can call it unconditionally after a selection. */
  dismiss: () => void;
}

const NONE: MissionRailSlot = { el: null, headEl: null, footEl: null, dismiss: () => {} };

const Ctx = createContext<MissionRailSlot>(NONE);

export const MissionRailSlotProvider = Ctx.Provider;

export function useMissionRailSlot(): MissionRailSlot {
  return useContext(Ctx);
}
