import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { CornerDownLeft, Minus, Plus, RotateCcw, Undo2, type LucideIcon } from "lucide-react";
import styles from "./filePanel.module.css";

export type RowOp = "stage" | "unstage" | "revert" | "discard" | "send";

const ITEMS: Record<RowOp, { label: string; Icon: LucideIcon; bad?: boolean }> = {
  stage: { label: "Stage", Icon: Plus },
  unstage: { label: "Unstage", Icon: Minus },
  revert: { label: "Revert to last commit…", Icon: Undo2, bad: true },
  discard: { label: "Discard changes…", Icon: RotateCcw, bad: true },
  send: { label: "Add to message", Icon: CornerDownLeft },
};

/** Heights on a coarse pointer, used only to decide whether the menu opens below its trigger or
 *  above it — a row near the pinned commit foot would otherwise open its menu off the screen. */
const ITEM_H = 44;
const HEAD_H = 30;

/** One git row's actions on a coarse pointer (#950).
 *
 *  With a commit checkbox, a status letter and up to four 44px glyphs, a 390px row left the
 *  filename about seven characters. So on a coarse pointer the glyphs collapse into this one menu
 *  and the name keeps the width; on a fine pointer they stay inline. The items are the same
 *  actions with the same `data-git-op`, so a destructive one still goes through its confirmation.
 *
 *  Portalled to <body> for the reason `BranchMenu` is: `.terminal-pane` clips an in-tree popover.
 */
export function RowMenu({
  path,
  ops,
  busy,
  anchor,
  rect,
  onPick,
  onClose,
}: {
  path: string;
  ops: RowOp[];
  /** A write is in flight or a refresh is outstanding: every git action is inert, sending the
   *  path to the message is not. */
  busy: boolean;
  /** The ⋯ trigger; focus goes back to it on every close path. */
  anchor: HTMLElement | null;
  /** The trigger's geometry, measured by the parent at click time (see `BranchMenu`). */
  rect: { top: number; bottom: number; left: number; right: number };
  onPick: (op: RowOp) => void;
  onClose: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const name = path.split("/").pop() || path;
  const width = Math.min(260, window.innerWidth - 16);
  const height = HEAD_H + ops.length * ITEM_H + 8;
  const fitsBelow = rect.bottom + 2 + height <= window.innerHeight - 8;
  const pos = {
    top: fitsBelow ? rect.bottom + 2 : Math.max(8, rect.top - 2 - height),
    left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
    width,
  };

  const close = useCallback(() => {
    onClose();
    if (anchor && document.contains(anchor)) anchor.focus();
  }, [onClose, anchor]);

  const move = useCallback((delta: number) => {
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>("[data-menu-item]:not(:disabled)") ?? [],
    );
    if (!items.length) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next = at < 0 ? 0 : (at + delta + items.length) % items.length;
    items[next]?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "Tab") {
        // Tab closes a menu rather than walking out of it behind a still-open popover.
        e.preventDefault();
        e.stopPropagation();
        close();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        move(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        move(-1);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [close, move]);

  useEffect(() => {
    menuRef.current?.querySelector<HTMLElement>("[data-menu-item]:not(:disabled)")?.focus();
  }, []);

  return createPortal(
    <>
      <button
        type="button"
        className={styles.menuScrim}
        aria-label={`Close the actions for ${name}`}
        onClick={close}
      />
      <div
        ref={menuRef}
        className={`${styles.headMenu} ${styles.aboveSheet}`}
        role="menu"
        aria-label={`Actions for ${path}`}
        data-row-menu={path}
        style={{ top: pos.top, left: pos.left, width: pos.width }}
      >
        <div className={styles.menuHead}>
          <span className="hud-tag">{name}</span>
        </div>
        {ops.map((op) => {
          const { label, Icon, bad } = ITEMS[op];
          return (
            <button
              key={op}
              type="button"
              role="menuitem"
              data-menu-item=""
              {...(op === "send" ? { "data-row-send": "" } : { "data-git-op": op })}
              className={`${styles.headMenuItem} ${bad ? styles.headMenuItemBad : ""}`}
              disabled={busy && op !== "send"}
              onClick={() => {
                close();
                onPick(op);
              }}
            >
              <Icon size={13} aria-hidden="true" />
              <span className={styles.menuItemText}>{label}</span>
            </button>
          );
        })}
      </div>
    </>,
    document.body,
  );
}
