/** A small menu anchored under one trigger — the contract `MissionOverflow` proved (#942, #967),
 *  shared so the Help menu (#987) is not a second copy of it.
 *
 *  **It is a real menu, not a div that appears.** `role="menu"`, focus moved in, Escape closes,
 *  focus returns to the trigger, and a press outside dismisses — supplied by `useModalDrawer`,
 *  the same hook the notification bell and the shell drawer use. Writing a fifth ad-hoc popover
 *  is how a codebase ends up with five subtly different ones.
 *
 *  **And `role="menu"` is a promise about the ARROW KEYS**, not just a label: Up/Down move between
 *  items, Home/End jump to the ends, and the menu is ONE tab stop rather than N. Disabled items
 *  are skipped, which is why the query filters them rather than indexing blindly.
 *
 *  **And Tab LEAVES.** That takes two things, not one: the handler below (which closes on Tab and
 *  lets `useModalDrawer`'s own restore put focus back on the trigger, outside the menu), and
 *  `containFocus: false`. The hook installs its Tab-containment listener independently of the
 *  isolation, so "isolate nothing" alone would leave the menu carrying a modal focus trap.
 *
 *  It is deliberately NOT `aria-modal`: this is a small anchored menu over a page that stays
 *  usable, not a surface that owns the screen.
 *
 *  **Where focus lands on open is the caller's decision** (#987 review). The mission menu focuses
 *  the menu wrapper, because which of its items comes first depends on the mission's state and
 *  some of them are destructive. The Help menu has no such item, so it focuses the first one.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useModalDrawer } from "../pulse/useModalDrawer";

/** The menu's own items, in DOM order, minus the ones that cannot be actioned. Read from the DOM
 *  rather than from a list prop because WHICH items exist is the caller's rendering to decide — a
 *  parallel list here would be a second copy of that map, free to drift from the one that renders. */
function menuItems(panel: HTMLElement | null): HTMLElement[] {
  if (!panel) return [];
  return Array.from(
    panel.querySelectorAll<HTMLElement>('[role="menuitem"]'),
  ).filter((n) => !n.hasAttribute("disabled"));
}

export function AnchoredMenu({
  label,
  trigger,
  triggerClassName,
  triggerTestId,
  menuTestId,
  disabled,
  focus = "menu",
  portal = false,
  classes,
  head,
  note,
  children,
}: {
  /** The accessible name of both the trigger and the menu. */
  label: string;
  /** The trigger's content — an icon. */
  trigger: ReactNode;
  triggerClassName: string;
  triggerTestId?: string;
  menuTestId?: string;
  disabled?: boolean;
  /** `"menu"` focuses the menu wrapper on open; `"first-item"` its first enabled item. */
  focus?: "menu" | "first-item";
  /** Render the panel into `<body>` at the trigger's measured position, right-aligned to it.
   *
   *  For a trigger inside `.hud-topbar`, which is not optional: on desktop the topbar has
   *  `backdrop-filter` and no `z-index`, so it is a stacking context the terminal pane paints over,
   *  and no `z-index` on the panel can lift it above a sibling that already beats its parent. The
   *  panel drew, took focus, and a click on it landed on the pane underneath (#987; the bell's
   *  #752). Portalling it out removes the stacking dependency instead of trying to out-number it —
   *  the notification bell's fix. The portalled panel declares `data-modal-inside`, so a drawer it
   *  was opened from does not read a press on it as a press outside. */
  portal?: boolean;
  /** Layout belongs to the caller: the mission header and the top bar anchor differently. */
  classes: { wrap: string; panel: string; items: string };
  /** A block ABOVE the items — the operator menu's "who is signed in" header (#1058). Like `note`
   *  it sits in the panel but OUTSIDE `role="menu"`, whose children may only be menu items. */
  head?: ReactNode;
  /** A sentence under the items. It sits in the panel but OUTSIDE `role="menu"`, whose children
   *  may only be menu items. */
  note?: ReactNode;
  /** The items. A function receives `close`, for items that act and are done; the mission menu
   *  passes plain nodes, because its two-tap confirmations keep the menu open. */
  children: ReactNode | ((close: () => void) => ReactNode);
}) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(
    null,
  );
  const panelRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const close = () => setOpen(false);

  const measure = useCallback(() => {
    const r = triggerRef.current?.getBoundingClientRect();
    if (r) setAnchor({ top: r.bottom + 4, right: window.innerWidth - r.right });
  }, []);

  // Re-measure while open: the topbar does not scroll, but a resize moves the trigger.
  useEffect(() => {
    if (!open || !portal) return;
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [open, portal, measure]);

  function onKeyDown(e: React.KeyboardEvent) {
    // TAB LEAVES, it does not wrap (#942 review). The page behind the menu is live, so tabbing away
    // is a legitimate thing to do and the menu's job is to get out of the way. It closes, and
    // `useModalDrawer`'s restore puts focus back on the trigger, from which the next Tab continues
    // into the page. Both directions, because a trap that holds one way is still a trap.
    if (e.key === "Tab") {
      e.preventDefault();
      setOpen(false);
      return;
    }
    const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const list = menuItems(panelRef.current);
    if (!list.length) return;
    e.preventDefault();
    // `document.activeElement` may be the wrapper focused on open, which is not in the list —
    // `indexOf` then answers -1, and -1 + 1 is 0, so Down lands on the first item. That is the
    // behaviour the pattern asks for, and it falls out rather than needing a branch.
    const at = list.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? list.length - 1
          : e.key === "ArrowDown"
            ? (at + 1) % list.length
            : (at <= 0 ? list.length : at) - 1;
    list[next]?.focus();
  }

  useModalDrawer({
    active: open,
    panelRef,
    initialFocusRef: menuRef,
    triggerRef,
    onClose: close,
    // THE TRIGGER IS INSIDE, so it can close what it opened (#942 review 3). Without this the hook
    // read a mousedown on the trigger as an outside press and closed the menu, and the click that
    // followed the same press toggled `open` back on — so two real clicks left the menu OPEN.
    insideRefs: [triggerRef],
    // The background stays live. Naming an empty set is what says "this panel isolates nothing",
    // rather than inheriting the default that makes `#root` inert.
    inertRefs: [],
    // …AND FOCUS IS NOT CONTAINED, which is a SECOND thing and not implied by the first (#942
    // review). Tab is handled above instead: it closes.
    containFocus: false,
  });

  // Declared after the hook, so it runs after the hook's own focus-in and has the last word.
  useEffect(() => {
    if (!open || focus !== "first-item") return;
    menuItems(panelRef.current)[0]?.focus();
  }, [open, focus]);

  const panel = open ? (
    <div
      ref={panelRef}
      className={classes.panel}
      data-testid={menuTestId}
      data-modal-inside={portal ? "" : undefined}
      style={
        portal && anchor
          ? { position: "fixed", top: anchor.top, right: anchor.right, left: "auto" }
          : undefined
      }
      onKeyDown={onKeyDown}
    >
      {head}
      {/* The focus target on open when the caller asks for "menu", and the menu itself. */}
      <div
        ref={menuRef}
        tabIndex={-1}
        className={classes.items}
        role="menu"
        aria-label={label}
      >
        {typeof children === "function" ? children(close) : children}
      </div>
      {note}
    </div>
  ) : null;

  return (
    <span className={classes.wrap}>
      <button
        type="button"
        ref={triggerRef}
        className={triggerClassName}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        onClick={() => {
          // Measured BEFORE it opens, so a portalled panel's first paint is already in place.
          if (!open && portal) measure();
          setOpen((v) => !v);
        }}
        data-testid={triggerTestId}
      >
        {trigger}
      </button>
      {panel && portal ? createPortal(panel, document.body) : panel}
    </span>
  );
}
