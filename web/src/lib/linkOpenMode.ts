/** How a session link that arrived from outside the app opens on THIS device (#1232).
 *
 *  Device-local, like the sidebar width and the terminal text size: the map exists only on a
 *  desktop-sized viewport, so a choice made on a desktop must not follow the operator to a phone.
 *  `"ask"` is the default and the reset (Settings → Appearance → Opening links). */
export type LinkOpenMode = "ask" | "fullscreen" | "map";

export const LINK_OPEN_MODE_KEY = "battlelab.linkOpenMode";

const MODES: readonly LinkOpenMode[] = ["ask", "fullscreen", "map"];

export function readLinkOpenMode(): LinkOpenMode {
  try {
    const v = localStorage.getItem(LINK_OPEN_MODE_KEY);
    return MODES.includes(v as LinkOpenMode) ? (v as LinkOpenMode) : "ask";
  } catch {
    return "ask";
  }
}

export function writeLinkOpenMode(mode: LinkOpenMode): void {
  try {
    if (mode === "ask") localStorage.removeItem(LINK_OPEN_MODE_KEY);
    else localStorage.setItem(LINK_OPEN_MODE_KEY, mode);
  } catch {
    // Storage refused (private mode, quota): the choice lasts for this page only, and the next
    // link asks again — the safe direction.
  }
}
