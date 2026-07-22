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
}

/** Items list may include "separator" markers between logical groups. */
export type RowMenuEntry = RowMenuItem | "separator";

interface RowMenuProps {
  items: RowMenuEntry[];
  /** Session title — shown in the mobile bottom-sheet header. */
  title?: string;
  /** Replaces the ⋯ glyph while a background action runs (e.g. spinning Sparkles). */
  triggerIcon?: ReactNode;
  triggerLabel?: string;
  /** Lets the row keep its hover-revealed action cluster visible while open. */
  onOpenChange?: (open: boolean) => void;
}

const GAP = 4; // px between trigger and menu
const EDGE = 8; // min distance from viewport edges

/** Single ⋯ trigger + context menu replacing the sidebar row's inline icon cluster
 *  (#384). The menu is portaled to <body> so the sidebar's overflow-y:auto scroll
 *  container can't clip it; on narrow viewports CSS turns it into a bottom sheet
 *  (see RowMenu.module.css). Keyboard: trigger opens & focuses the first item,
 *  Arrow keys cycle, Home/End jump, Esc/Tab/outside-click close (scroll/resize close the
 *  desktop popover only — the mobile sheet is viewport-pinned, see the close effect),
 *  Esc returns focus to the trigger. */
export function RowMenu({
  items,
  title,
  triggerIcon,
  triggerLabel = "Session actions",
  onOpenChange,
}: RowMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const setOpenNotify = useCallback(
    (next: boolean) => {
      setOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  const close = useCallback(
    (refocus: boolean) => {
      setOpenNotify(false);
      if (refocus) triggerRef.current?.focus();
    },
    [setOpenNotify],
  );

  // Anchor the popover under the trigger, right-aligned; flip above when there is
  // no room below. Exposed as CSS custom props (not top/right inline styles) so the
  // mobile media query can win and pin the menu to the bottom instead.
  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const rect = trigger.getBoundingClientRect();
    const menuH = menu.offsetHeight;
    const below = rect.bottom + GAP;
    const flip = below + menuH > window.innerHeight - EDGE && rect.top - GAP - menuH > EDGE;
    const top = flip ? rect.top - GAP - menuH : below;
    menu.style.setProperty("--rm-top", `${Math.max(EDGE, top)}px`);
    menu.style.setProperty("--rm-right", `${Math.max(EDGE, window.innerWidth - rect.right)}px`);
  }, [open]);

  // Focus the first item on open (menu-button pattern).
  useEffect(() => {
    if (open) itemRefs.current[0]?.focus();
  }, [open]);

  // Close on an outside pointerdown (both modes). On DESKTOP the menu is a popover anchored to
  // the trigger, so a scroll or viewport resize that slides the trigger out from under it must
  // dismiss it too. The MOBILE bottom sheet is pinned to the viewport (scrim-locked behind), so
  // it must NOT bind scroll/resize: a mobile browser shows/hides its URL bar on the very tap
  // that opens the sheet, firing resize (and scroll) — which would flicker the sheet shut the
  // instant you press ⋯. Gate those two on desktop only.
  useEffect(() => {
    if (!open) return;
    const isSheet =
      typeof window.matchMedia === "function" && window.matchMedia("(max-width: 800px)").matches;
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      close(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    let detachViewport = () => {};
    if (!isSheet) {
      const onScroll = (e: Event) => {
        if (menuRef.current?.contains(e.target as Node)) return;
        close(false);
      };
      const onResize = () => close(false);
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
  }, [open, close]);

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    const focusable = itemRefs.current.filter(Boolean) as HTMLButtonElement[];
    const cur = focusable.indexOf(document.activeElement as HTMLButtonElement);
    const focusAt = (i: number) => focusable[(i + focusable.length) % focusable.length]?.focus();
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
        close(true);
        break;
      case "Tab":
        // APG: Tab closes the menu and lets focus move on naturally.
        close(false);
        break;
    }
  };

  const select = (item: RowMenuItem) => {
    if (item.disabled) return;
    // Close + refocus the trigger first: if the action swaps the row into another
    // surface (rename → autofocused edit input), that surface then wins focus.
    close(true);
    item.onSelect();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-label={triggerLabel}
        title={triggerLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpenNotify(!open)}
      >
        {triggerIcon ?? <MoreHorizontal size={16} />}
      </button>
      {open &&
        createPortal(
          <>
            {/* Mobile-only scrim behind the bottom sheet (display:none on desktop). */}
            <div className={styles.scrim} aria-hidden="true" onClick={() => close(false)} />
            {/* Sheet wrapper: `display:contents` on desktop (the menu stays a fixed popover),
                but on mobile a click-through, dynamic-viewport-height flex box that pins the
                sheet to the *visible* bottom — above the browser's collapsing toolbar — so the
                lower actions + Cancel can't hide behind it (the sheet itself scrolls when tall). */}
            <div className={styles.sheetWrap}>
              <div
                ref={menuRef}
                className={styles.menu}
                role="menu"
                aria-label={triggerLabel}
                onKeyDown={onMenuKeyDown}
              >
                {title && (
                  <div className={styles.sheetHead} aria-hidden="true">
                    <div className={styles.sheetTitle}>Session actions</div>
                    <div className={styles.sheetSession}>{title}</div>
                  </div>
                )}
                {items.map((entry, i) => {
                  if (entry === "separator") {
                    return <div key={`sep-${i}`} className={styles.sep} role="separator" />;
                  }
                  // Roving-focus slot: position among action items only (separators skipped).
                  const idx = items.slice(0, i).filter((it) => it !== "separator").length;
                  return (
                    <button
                      key={entry.key}
                      ref={(el) => {
                        itemRefs.current[idx] = el;
                      }}
                      type="button"
                      role="menuitem"
                      tabIndex={-1}
                      className={styles.item}
                      aria-label={entry.ariaLabel}
                      aria-disabled={entry.disabled || undefined}
                      onClick={() => select(entry)}
                    >
                      <span className={styles.itemIcon}>{entry.icon}</span>
                      {entry.label}
                    </button>
                  );
                })}
                <button
                  type="button"
                  className={styles.cancel}
                  tabIndex={-1}
                  onClick={() => close(true)}
                >
                  Cancel
                </button>
              </div>
            </div>
          </>,
          document.body,
        )}
    </>
  );
}
