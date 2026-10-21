/** The link this document was OPENED on, if it was a BattleLab session or mission link (#1232).
 *
 *  Captured once, at module load, from a fresh `navigate` — never a reload or a back/forward,
 *  which put the operator back where they already were and must not ask them anything. Read by
 *  `main.tsx` (the tab hand-off) and by `EntryLinks` (the open-mode prompt). */
import { classifyLink, type LinkEntry } from "./linkEntry";

function navigationType(): string | undefined {
  const nav = performance.getEntriesByType?.("navigation")[0] as
    | PerformanceNavigationTiming
    | undefined;
  return nav?.type;
}

/** Opened FROM BattleLab itself — a ⌘/ctrl/middle-click on a session row asks for a new tab on
 *  purpose, and handing that link back to the tab it came from would undo the request.
 *
 *  A link opened while signed out lands here after `/login`, whose referrer is this origin too, so
 *  it opens full screen without asking or handing off. That errs on the side of doing nothing
 *  surprising, and a signed-out browser rarely has another BattleLab tab to hand to. */
function openedFromHere(): boolean {
  try {
    return document.referrer !== "" && new URL(document.referrer).origin === window.location.origin;
  } catch {
    return false;
  }
}

function capture(): LinkEntry | null {
  if (typeof window === "undefined") return null;
  if (navigationType() !== "navigate" || openedFromHere()) return null;
  return classifyLink(
    window.location.pathname + window.location.search,
    window.location.origin,
  );
}

let entry: LinkEntry | null = capture();
let consumed = false;

/** The boot entry, handed out ONCE: the first caller that acts on it owns it. */
export function takeBootEntry(): LinkEntry | null {
  if (consumed) return null;
  consumed = true;
  return entry;
}

export function peekBootEntry(): LinkEntry | null {
  return consumed ? null : entry;
}

/** Tests only. */
export function resetBootEntryForTest(next: LinkEntry | null): void {
  entry = next;
  consumed = false;
}

/** The installed app (`display-mode: standalone`), where `launch_handler` owns link routing. */
export function isStandalone(): boolean {
  return window.matchMedia?.("(display-mode: standalone)").matches ?? false;
}
