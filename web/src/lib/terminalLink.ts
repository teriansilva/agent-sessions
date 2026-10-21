/** Open a URL clicked in agent output (#158, #1232).
 *
 *  A BattleLab session or mission link on THIS origin opens right here, through the same
 *  full-screen / map decision as a link from outside (`EntryLinks` listens for the event). Agents
 *  print these links all the time — every session record does — and opening one in a new tab would
 *  hand it straight back to this tab anyway (`linkHandoff`), leaving a stub behind.
 *
 *  Anything else opens in a new tab, denying `window.opener` (the linked page cannot navigate this
 *  tab) and the Referer (privacy). A ⌘/ctrl-click on a BattleLab link asks for a new tab on
 *  purpose, so it keeps the same-origin referrer that tells the new tab not to hand it back. */
import { classifyLink, type LinkEntry } from "./linkEntry";

export const IN_APP_LINK_EVENT = "battlelab:open-link";

export function openTerminalLink(uri: string, newTab = false): void {
  const entry = classifyLink(uri, window.location.origin);
  if (entry && !newTab) {
    window.dispatchEvent(new CustomEvent<LinkEntry>(IN_APP_LINK_EVENT, { detail: entry }));
    return;
  }
  window.open(uri, "_blank", entry ? "noopener" : "noopener,noreferrer");
}
