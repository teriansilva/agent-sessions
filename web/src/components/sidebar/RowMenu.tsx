import { MoreHorizontal } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import styles from "./RowMenu.module.css";

/** One action in the row's ⋯ menu (#384). `label` is the visible text; `ariaLabel`
 *  keeps the pre-menu accessible names ("Rename session", …) stable for AT users
 *  and tests. A disabled item stays in the list (aria-disabled, dimmed) so the menu
 *  doesn't reflow mid-action. */
export interface RowMenuItem {
  key: string;
  label: string;
  ariaLabel?: string;
  icon: ReactNode;
  disabled?: boolean;
  onSelect: () => void;
  /** A second, smaller line under the label — e.g. why a disabled item is disabled (#967). */
  hint?: string;
  /** Destructive: drawn in `--danger-text` (#967). */
  danger?: boolean;
  /** `data-*` attributes for the item's button, e.g. a `data-testid` a surface already pins, or
   *  a value the item acts on that a browser test reads back (#967). */
  data?: Record<`data-${string}`, string | number>;
}

/** A labelled group header inside a menu (#1109): the merged window ⋯ menu separates its two
 *  item sources — the session's own actions and the pane's folded head actions — with a small
 *  HUD-voice label rather than an ambiguous rule. Non-interactive, aria-hidden (the groups'
 *  items carry their own names). */
export interface RowMenuGroup {
  group: string;
}

/** Items list may include "separator" markers between logical groups and `RowMenuGroup`
 *  headers naming them. */
export type RowMenuEntry = RowMenuItem | "separator" | RowMenuGroup;

/** True when an entry is NOT an actionable item (a separator marker or a group label) — the
 *  roving-focus arithmetic must skip both. Module-local: only the render below reads it. */
function isMenuNonItem(entry: RowMenuEntry): entry is "separator" | RowMenuGroup {
  return typeof entry === "string" || "group" in entry;
}

/** Where the popover opens (#968).
 *  - `element`: under that element, right-aligned, flipped above when there is no room below —
 *    the ⋯ trigger's placement. Measured when the menu opens, so a trigger inside a moving
 *    surface (a zoomed map chip) is read where it is, not where it was.
 *  - `point`: at a pointer position, the context-menu convention — its top-left corner on the
 *    pointer, flipped left and/or up to stay on screen. */
export type MenuAnchor = { element: HTMLElement } | { point: { x: number; y: number } };

/** The mobile bottom sheet's heading when the caller does not name one. Every menu before #967 was
 *  a session's, so this was hard-coded; a caller acting on something else passes `sheetTitle`. */
const DEFAULT_SHEET_TITLE = "Session actions";

interface MenuPopoverProps {
  items: RowMenuEntry[];
  anchor: MenuAnchor;
  /** Session title — shown in the mobile bottom-sheet header. */
  title?: string;
  /** The bottom sheet's heading above `title` (#967). Defaults to "Session actions". */
  sheetTitle?: string;
  label?: string;
  /** `refocus` is true when the menu closed through the keyboard or an item (the menu-button
   *  pattern returns focus); false for an outside press, scroll or resize. */
  onClose: (refocus: boolean) => void;
  /** A press on this element is not an outside press — the trigger toggles the menu itself. */
  ownerRef?: React.RefObject<HTMLElement | null>;
}

const GAP = 4; // px between trigger and menu
const EDGE = 8; // min distance from viewport edges

/** The open menu itself, portaled to <body> (#384) so no scroll container or React Flow transform
 *  can clip or scale it; on narrow viewports CSS turns it into a bottom sheet (see
 *  RowMenu.module.css). Shared by the sidebar row's ⋯ and the Overview map (#968). Keyboard:
 *  focuses the first item, Arrow keys cycle, Home/End jump, Esc/Tab/outside-press close
 *  (scroll/resize close the desktop popover only — the mobile sheet is viewport-pinned, see the
 *  close effect). */
export function MenuPopover({
  items,
  anchor,
  title,
  sheetTitle = DEFAULT_SHEET_TITLE,
  label = "Session actions",
  onClose,
  ownerRef,
}: MenuPopoverProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  // The anchor is read once, at open: a context menu does not chase a moving target.
  const anchorRef = useRef(anchor);

  // Exposed as CSS custom props (not top/right inline styles) so the mobile media query can win
  // and pin the menu to the bottom instead.
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const menuH = menu.offsetHeight;
    const menuW = menu.offsetWidth;
    const a = anchorRef.current;
    let top: number;
    let right: number;
    if ("element" in a) {
      const rect = a.element.getBoundingClientRect();
      const below = rect.bottom + GAP;
      const flip =
        below + menuH > window.innerHeight - EDGE &&
        rect.top - GAP - menuH > EDGE;
      top = flip ? rect.top - GAP - menuH : below;
      right = window.innerWidth - rect.right;
    } else {
      // A `contextmenu` can arrive without pointer coordinates (a synthesized event, some
      // assistive paths). NaN would place the menu off-screen with nothing to dismiss it by, so a
      // coordinate that is not a number is the viewport's edge instead.
      const x = Number.isFinite(a.point.x) ? a.point.x : EDGE;
      const y = Number.isFinite(a.point.y) ? a.point.y : EDGE;
      const fitsRight = x + menuW <= window.innerWidth - EDGE;
      const left = fitsRight ? x : x - menuW;
      right = window.innerWidth - (left + menuW);
      top = y + menuH <= window.innerHeight - EDGE ? y : y - menuH;
    }
    // Vertical clamp (#1109 review): when the menu fits NEITHER above nor below the anchor
    // (a short viewport, or a Session+Pane merged menu opened near the bottom of a panned
    // map), the fallback below used to run the menu's tail past the viewport — with the
    // text-size controls unreachable and nothing visible to dismiss by. The CSS caps the
    // menu at the viewport's height (scrolling inside), so this clamp always has room; the
    // menu never starts above EDGE and never ends past the bottom edge.
    const maxTop = Math.max(EDGE, window.innerHeight - EDGE - menuH);
    menu.style.setProperty(
      "--rm-top",
      `${Math.min(Math.max(EDGE, top), maxTop)}px`,
    );
    // Both horizontal edges (#968 review). The sidebar's ⋯ always sat far from the left edge, so
    // only the right inset was clamped; a ⋯ on a panned map chip can sit anywhere, and a menu
    // right-aligned under one near the left edge started off-screen. `right` may not exceed the
    // inset that keeps the menu's LEFT edge at EDGE; a menu wider than the viewport pins right.
    const maxRight = Math.max(EDGE, window.innerWidth - EDGE - menuW);
    menu.style.setProperty(
      "--rm-right",
      `${Math.min(maxRight, Math.max(EDGE, right))}px`,
    );
  }, []);

  // Focus the first item on open (menu-button pattern).
  useEffect(() => {
    itemRefs.current[0]?.focus();
  }, []);

  // Close on an outside pointerdown (both modes). On DESKTOP the menu is a popover anchored to
  // the trigger, so a scroll or viewport resize that slides the trigger out from under it must
  // dismiss it too. The MOBILE bottom sheet is pinned to the viewport (scrim-locked behind), so
  // it must NOT bind scroll/resize: a mobile browser shows/hides its URL bar on the very tap
  // that opens the sheet, firing resize (and scroll) — which would flicker the sheet shut the
  // instant you press ⋯. Gate those two on desktop only.
  useEffect(() => {
    const isSheet =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 800px)").matches;
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || ownerRef?.current?.contains(t))
        return;
      onClose(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    let detachViewport = () => {};
    if (!isSheet) {
      const onScroll = (e: Event) => {
        if (menuRef.current?.contains(e.target as Node)) return;
        onClose(false);
      };
      const onResize = () => onClose(false);
      // capture: the sidebar list scrolls its own box, not the window
      window.addEventListener("scroll", onScroll, true);
      window.addEventListener("resize", onResize);
      detachViewport = () => {
        window.removeEventListener("scroll", onScroll, true);
        window.removeEventListener("resize", onResize);
      };
    }
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      detachViewport();
    };
  }, [onClose, ownerRef]);

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    const focusable = itemRefs.current.filter(Boolean) as HTMLButtonElement[];
    const cur = focusable.indexOf(document.activeElement as HTMLButtonElement);
    const focusAt = (i: number) =>
      focusable[(i + focusable.length) % focusable.length]?.focus();
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        focusAt(cur + 1);
        break;
      case "ArrowUp":
        e.preventDefault();
        focusAt(cur - 1);
        break;
      case "Home":
        e.preventDefault();
        focusAt(0);
        break;
      case "End":
        e.preventDefault();
        focusAt(focusable.length - 1);
        break;
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        onClose(true);
        break;
      case "Tab":
        // APG: Tab closes the menu and lets focus move on naturally.
        onClose(false);
        break;
    }
  };

  const select = (item: RowMenuItem) => {
    if (item.disabled) return;
    // Close + refocus first: if the action swaps the row into another surface (rename →
    // autofocused edit input, a dialog), that surface then wins focus.
    onClose(true);
    item.onSelect();
  };

  return createPortal(
    <>
      {/* Mobile-only scrim behind the bottom sheet (display:none on desktop). */}
      <div
        className={styles.scrim}
        aria-hidden="true"
        onClick={() => onClose(false)}
      />
      {/* Sheet wrapper: `display:contents` on desktop (the menu stays a fixed popover),
          but on mobile a click-through, dynamic-viewport-height flex box that pins the
          sheet to the *visible* bottom — above the browser's collapsing toolbar — so the
          lower actions + Cancel can't hide behind it (the sheet itself scrolls when tall). */}
      {/* PART OF WHATEVER PANEL OPENED IT (#940 review 1). This subtree is portalled to
          <body>, so a modal drawer testing DOM containment sees a press here as a press
          outside itself and dismisses — taking the host of any inline editor this menu
          mounts with it. The attribute is the opt-in `useModalDrawer` looks for. */}
      <div className={styles.sheetWrap} data-modal-inside="">
        <div
          ref={menuRef}
          className={styles.menu}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
          // A right-click on the open menu must not raise the browser's own on top of it.
          onContextMenu={(e) => e.preventDefault()}
        >
          {title && (
            <div className={styles.sheetHead} aria-hidden="true">
              <div className={styles.sheetTitle}>{sheetTitle}</div>
              <div className={styles.sheetSession}>{title}</div>
            </div>
          )}
          {items.map((entry, i) => {
            if (entry === "separator") {
              return (
                <div
                  key={`sep-${i}`}
                  className={styles.sep}
                  role="separator"
                />
              );
            }
            if ("group" in entry) {
              return (
                <div
                  key={`grp-${i}`}
                  className={styles.groupLabel}
                  aria-hidden="true"
                  data-menu-group={entry.group}
                >
                  {entry.group}
                </div>
              );
            }
            // Roving-focus slot: position among action items only (separators skipped).
            const idx = items
              .slice(0, i)
              .filter((it) => !isMenuNonItem(it)).length;
            return (
              <button
                key={entry.key}
                {...entry.data}
                ref={(el) => {
                  itemRefs.current[idx] = el;
                }}
                type="button"
                role="menuitem"
                tabIndex={-1}
                className={
                  entry.danger
                    ? `${styles.item} ${styles.itemDanger}`
                    : styles.item
                }
                aria-label={entry.ariaLabel}
                aria-disabled={entry.disabled || undefined}
                onClick={() => select(entry)}
              >
                <span className={styles.itemIcon}>{entry.icon}</span>
                {/* Wrapped only when there is a hint, so every existing item keeps its DOM. */}
                {entry.hint ? (
                  <span className={styles.itemText}>
                    {entry.label}
                    <span className={styles.itemHint}>{entry.hint}</span>
                  </span>
                ) : (
                  entry.label
                )}
              </button>
            );
          })}
          <button
            type="button"
            className={styles.cancel}
            tabIndex={-1}
            onClick={() => onClose(true)}
          >
            Cancel
          </button>
        </div>
      </div>
    </>,
    document.body,
  );
}

interface RowMenuProps {
  items: RowMenuEntry[];
  /** Session title — shown in the mobile bottom-sheet header. */
  title?: string;
  /** The bottom sheet's heading above `title` (#967). Defaults to "Session actions". */
  sheetTitle?: string;
  /** Replaces the ⋯ glyph while a background action runs (e.g. spinning Sparkles). */
  triggerIcon?: ReactNode;
  triggerLabel?: string;
  /** Replaces the trigger's own class — for a surface whose ⋯ holds a different hit target, such
   *  as the 44×44 objective row trigger (#967). */
  triggerClassName?: string;
  triggerTestId?: string;
  /** Lets the row keep its hover-revealed action cluster visible while open. */
  onOpenChange?: (open: boolean) => void;
}

/** Single ⋯ trigger + context menu replacing the sidebar row's inline icon cluster
 *  (#384). The menu itself is `MenuPopover`, anchored to this trigger; Esc and item selection
 *  return focus here. */
export function RowMenu({
  items,
  title,
  sheetTitle,
  triggerIcon,
  triggerLabel = "Session actions",
  triggerClassName,
  triggerTestId,
  onOpenChange,
}: RowMenuProps) {
  // The open state IS the anchor: the trigger element the press landed on. Held in state rather
  // than read off the ref during render, so opening is an ordinary render input.
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const open = anchorEl !== null;
  const triggerRef = useRef<HTMLButtonElement>(null);

  const setOpenNotify = useCallback(
    (next: HTMLElement | null) => {
      setAnchorEl(next);
      onOpenChange?.(next !== null);
    },
    [onOpenChange],
  );

  const close = useCallback(
    (refocus: boolean) => {
      setOpenNotify(null);
      if (refocus) triggerRef.current?.focus();
    },
    [setOpenNotify],
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={triggerClassName ?? styles.trigger}
        aria-label={triggerLabel}
        title={triggerLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid={triggerTestId}
        onClick={(e) => setOpenNotify(open ? null : e.currentTarget)}
      >
        {triggerIcon ?? <MoreHorizontal size={16} />}
      </button>
      {anchorEl && (
        <MenuPopover
          items={items}
          title={title}
          sheetTitle={sheetTitle}
          label={triggerLabel}
          anchor={{ element: anchorEl }}
          onClose={close}
          ownerRef={triggerRef}
        />
      )}
    </>
  );
}
