/** The modal-drawer contract, in one place (#878).
 *
 * Extracted verbatim from `NotificationBell`, which implemented it inline and correctly. It is
 * shared rather than copied because a focus trap is exactly the kind of thing that rots when
 * duplicated: the second copy is written from memory of the first, drifts, and the drift is
 * invisible until an operator is trapped in a panel. `mobile-pulse-layout.spec.ts` exercises the
 * bell's drawer end-to-end and is the safety net for this extraction — if the hook is not
 * faithful, those tests say so.
 *
 * `aria-modal` is a PROMISE, and declaring it without honouring it is worse than not declaring
 * it at all: it tells assistive technology the background is inert while it is still reachable.
 * The promise has four parts, and all four live here:
 *
 *   1. focus moves INTO the panel on open;
 *   2. `#root` is made genuinely `inert` — the panel and scrim are portalled OUTSIDE `#root`,
 *      so the background is isolated without touching either;
 *   3. Tab is contained, which is what makes the trap observable in a test rather than inferred
 *      from an attribute;
 *   4. focus RETURNS to the trigger on every close path — Escape, scrim, an action inside, or a
 *      resize that drops drawer mode.
 */
import { useEffect, type RefObject } from "react";

export interface ModalDrawerOptions {
  /** Whether the drawer is open AND in drawer mode. A panel that is merely an anchored dropdown
   *  owes none of this, so both conditions are the caller's to combine. */
  active: boolean;
  /** The panel element — the focus-containment host. */
  panelRef: RefObject<HTMLElement | null>;
  /** Where focus goes on open, and what it is contained around. Usually the Close button:
   *  `#root` goes inert, so the trigger itself stops being clickable while open, which is why
   *  the drawer must carry its own close control. */
  initialFocusRef: RefObject<HTMLElement | null>;
  /** Focus returns here on close. */
  triggerRef: RefObject<HTMLElement | null>;
  /** Escape and scrim/outside-click both call this. */
  onClose: () => void;
  /** Regions that count as "inside" for outside-click purposes. The panel is portalled to
   *  <body>, so testing a wrapper alone would close it on every tap of its own rows. */
  insideRefs?: RefObject<HTMLElement | null>[];
}

export function useModalDrawer({
  active,
  panelRef,
  initialFocusRef,
  triggerRef,
  onClose,
  insideRefs = [],
}: ModalDrawerOptions): void {
  // Close on outside click / Escape — a panel that traps the operator is worse than no panel.
  useEffect(() => {
    if (!active) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t)) return;
      for (const r of insideRefs) if (r.current?.contains(t)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
    // `insideRefs` is a fresh array each render by construction; its REFS are stable, and the
    // handler reads them at event time, so re-subscribing on identity would churn for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, onClose, panelRef]);

  // Focus in, background inert, focus back out.
  useEffect(() => {
    if (!active) return;
    const trigger = triggerRef.current;
    const root = document.getElementById("root");
    initialFocusRef.current?.focus();
    root?.setAttribute("inert", "");
    return () => {
      // Order matters: the trigger lives inside #root and cannot take focus while it is inert.
      root?.removeAttribute("inert");
      // …and the restore waits a frame. On the Escape path a synchronous `focus()` sticks, but
      // when the drawer is dismissed by a TAP the browser is still settling focus from that
      // pointer sequence and finishes after this passive cleanup — landing on <body> and
      // silently undoing the restore. A frame later the event is done and the trigger keeps it.
      requestAnimationFrame(() => trigger?.focus());
    };
  }, [active, initialFocusRef, triggerRef]);

  // Tab containment. `inert` already stops the background taking focus in browsers that support
  // it; this keeps the cycle correct inside the drawer either way.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const host = panelRef.current;
      if (!host) return;
      const focusable = Array.from(
        host.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement;
      const inside = activeEl instanceof Node && host.contains(activeEl);
      if (e.shiftKey && (!inside || activeEl === first)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (!inside || activeEl === last)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active, panelRef]);
}
