/** Pop a session out into its own window or tab: the session alone, without the app shell around
 *  it (no session list, section nav, top bar or footer).
 *
 *  The pop-out is a second VIEWER of the session, exactly like opening it in another tab: it
 *  attaches, it never launches. So a `new-<uuid>` placeholder (a launch that has not reconciled)
 *  is never popped out — there would be nothing to attach to. */
import { createContext, useContext } from "react";
import { appUrl } from "./appBase";
import { parseSessionKey } from "./format";
import { POPOUT_PATH } from "./routes";

/** `/pop/<engine>/<id>` for an engine-qualified key, or null when the key is malformed. */
export function popoutPathFromKey(key: string): string | null {
  const p = parseSessionKey(key);
  return p
    ? `${POPOUT_PATH}/${encodeURIComponent(p.engine)}/${encodeURIComponent(p.uuid)}`
    : null;
}

/** The browser window name for a session's pop-out. Named per session, so popping the same
 *  session out again brings its window back instead of opening a second one. */
export function popoutWindowName(key: string): string {
  return `battlelab-pop-${key.replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

/** Window size a desktop browser opens the pop-out at. A phone opens it as a tab instead. */
export const POPOUT_FEATURES = "popup,width=1100,height=760";

/** Open `key` in its pop-out window. Must run inside the click that asked for it (popup blockers
 *  allow a `window.open` only with a user activation). Returns false when nothing was opened. */
export function popOutSession(key: string): boolean {
  const path = popoutPathFromKey(key);
  if (!path) return false;
  const win = window.open(appUrl(path), popoutWindowName(key), POPOUT_FEATURES);
  win?.focus();
  return win !== null;
}

/** True inside a pop-out window: the session menu does not offer to pop out what is already out,
 *  and the session view offers no "To map" (the map lives in the main window). */
export const PopoutContext = createContext(false);

export function useInPopout(): boolean {
  return useContext(PopoutContext);
}
