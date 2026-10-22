import { type RefObject, useEffect } from "react";

/** A modal dialog whose request disables every control while it runs (#1531, Hermes on #1560).
 *  A disabled button drops focus to the body — outside the modal, where Tab then walks the page
 *  behind it. When `busy` starts and focus is no longer on a live, enabled control inside the
 *  dialog, the dialog itself takes it. The dialog must be focusable: `tabIndex={-1}`. */
export function useHoldFocusWhileBusy(panelRef: RefObject<HTMLElement | null>, busy: boolean) {
  useEffect(() => {
    if (!busy) return;
    const panel = panelRef.current;
    if (!panel) return;
    const active = document.activeElement as HTMLElement | null;
    const held =
      active &&
      active !== document.body &&
      active.isConnected &&
      panel.contains(active) &&
      !(active as HTMLButtonElement).disabled;
    if (!held) panel.focus();
  }, [busy, panelRef]);
}
