import type { RowMenuEntry } from "../sidebar/RowMenu";
import type { HeadAction } from "../terminal/HeadActions";

/** The merged session menu's PANE half (#1109, shared in #2173): what a host that shows the
 *  session menu beside a mounted pane needs to build it. Two hosts, one rule: a map window's
 *  chrome ⋯ (`SessionWindow`) and the session pane's own "…" (`PaneHeadActions`). */

/** The session-menu items a pane action mirrors (#1329). Only the twins of actions the mounted
 *  pane actually offers are omitted from the merged menu's session group, so an action is never
 *  named twice: it is a chip when the bar had room, the Pane group when it did not. */
const PANE_TO_SESSION_KEYS: Record<string, string[]> = {
  recap: ["brief"],
  handoff: ["handoff"],
  mission: ["adopt-mission", "open-mission"],
  // #2173: the pane's one Share action carries both choices, as a chip menu or a folded group.
  share: ["share-public", "share-peer"],
};

/** The session-menu keys to omit for a pane offering `offered` (its FULL action list, not the
 *  overflow: the overflow cannot say whether an action the fold kept on the bar is offered at
 *  all). A pane that offers nothing omits nothing. */
export function omittedSessionKeys(offered: readonly HeadAction[]): Set<string> {
  const ids = new Set(offered.map((a) => a.id));
  const omit = new Set<string>();
  for (const [paneId, keys] of Object.entries(PANE_TO_SESSION_KEYS)) {
    if (!ids.has(paneId)) continue;
    for (const k of keys) omit.add(k);
  }
  return omit;
}

/** The folded pane actions as menu entries. `opener` is the trigger the menu returns focus to:
 *  a menu item unmounts with its menu, so whatever an action opens is handed the trigger. A
 *  folded group of choices (#2173, Share) is its label once, then its items, ruled off from the
 *  pane actions on either side. No nested fly-out. */
export function paneMenuEntries(
  folded: readonly HeadAction[],
  opener: HTMLElement | null,
): RowMenuEntry[] {
  const entry = (a: HeadAction): RowMenuEntry => ({
    key: a.id,
    label: a.label,
    ariaLabel: a.aria,
    icon: a.icon,
    disabled: a.disabled,
    // A pressed pane toggle (Files, Plan & tools) says so in the menu too (#1726); an item
    // that is off says why (#2173).
    hint: a.active ? "Shown" : a.hint,
    data: { "data-head-menu-item": a.id },
    onSelect: () => a.run(opener),
  });
  const out: RowMenuEntry[] = [];
  folded.forEach((a, i) => {
    if (!a.menu?.length) {
      out.push(entry(a));
      return;
    }
    if (out.length > 0 && out[out.length - 1] !== "separator") out.push("separator");
    out.push({ group: a.label }, ...a.menu.map(entry));
    if (i < folded.length - 1) out.push("separator");
  });
  return out;
}
