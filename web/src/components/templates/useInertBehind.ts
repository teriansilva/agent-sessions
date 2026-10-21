import { useEffect } from "react";

/** The page behind an `aria-modal` dialog is inert while it is open — nothing behind it can be
 *  focused or activated, so Tab cannot walk out of the dialog (the ConfirmDialog rule). The
 *  dialog itself is portalled to <body>, outside the app root, so it stays live.
 *
 *  On close the root is released FIRST and focus returns AFTER: a restore attempted while the
 *  root is still inert is refused by the browser and lands on document.body (Hermes on #908,
 *  round 7 — jsdom never enforces inert, so only a real browser showed it). A trigger that
 *  unmounted meanwhile (a KeyBar "…" overflow item goes the moment it is clicked) cannot take
 *  focus either; the composer's stable "More keys" trigger stands in for it. */
export function useInertBehind(returnFocusTo?: HTMLElement | null) {
  useEffect(() => {
    const root = document.getElementById("root");
    const mine = !!root && !root.hasAttribute("inert"); // a nested dialog leaves the outer one's mark
    if (mine) root.setAttribute("inert", "");
    return () => {
      if (mine) root.removeAttribute("inert");
      const el = returnFocusTo?.isConnected
        ? returnFocusTo
        : document.querySelector<HTMLElement>('button[aria-label="More keys"]');
      el?.focus?.();
    };
  }, [returnFocusTo]);
}
