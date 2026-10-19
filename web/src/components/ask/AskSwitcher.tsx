/** The conversation switcher in Ask's head (#1443). It takes the head's spare room and shows the
 *  current conversation's title, truncated; it opens a list of the kept conversations, latest
 *  first (the mockup's `.switch` / `.menu`).
 *
 *  **Keyboard.** Opening moves focus INTO the list, onto the current conversation (or the first).
 *  Arrow keys, Home and End move between rows; Enter or Space opens one; Escape and Tab close the
 *  list and hand focus back to the switcher. Escape is answered here and goes no further: on a
 *  desktop the panel's own Escape would close Ask, and in the phone drawer the list declares
 *  `data-modal-inside`, which is what the drawer's Escape stands down for.
 *
 *  A click outside closes it. It is not portalled: the sidebar already portals itself to `<body>`
 *  above the top bar's stacking context, so the list can sit inside the panel.
 */
import { ChevronDown, History } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import type { AskConversationSummary } from "../../types/api";

import a from "./AskConsole.module.css";

function when(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

export function AskSwitcher({
  conversations,
  currentId,
  currentTitle,
  onOpen,
  onPick,
}: {
  /** Latest first; `null` until the first read. */
  conversations: AskConversationSummary[] | null;
  currentId: string | null;
  currentTitle: string;
  /** Opening asks for a fresh list. */
  onOpen: () => void;
  onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  /** The active row by conversation ID, never by index: a refresh may reorder the rows while the
   *  list is open, and Enter must open the row that has focus. */
  const [activeId, setActiveId] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const rows = conversations ?? [];
  const listId = useId();
  const found = rows.findIndex((c) => c.id === activeId);
  const active = found >= 0 ? found : 0;
  const order = rows.map((c) => c.id).join(" ");
  const moveTo = (i: number) => {
    const row = rows[Math.max(0, Math.min(rows.length - 1, i))];
    if (row) setActiveId(row.id);
  };

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  };

  const show = () => {
    onOpen();
    setActiveId(rows.some((c) => c.id === currentId) ? currentId : (rows[0]?.id ?? null));
    setOpen(true);
  };

  // Focus follows the active row while the list is open — a roving tabindex. Re-run on a reorder
  // too (`order`), so the focused node is always the active conversation's.
  useEffect(() => {
    if (!open) return;
    const items = listRef.current?.querySelectorAll<HTMLElement>('[role="option"]');
    if (items && items.length) items[Math.min(active, items.length - 1)]?.focus();
    else listRef.current?.focus();
  }, [open, active, order]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (listRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const last = rows.length - 1;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === "Tab") {
      close(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      moveTo(active + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      moveTo(active - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      moveTo(0);
    } else if (e.key === "End") {
      e.preventDefault();
      moveTo(last);
    } else if ((e.key === "Enter" || e.key === " ") && rows[active]) {
      e.preventDefault();
      close(true);
      onPick(rows[active].id);
    }
  };

  return (
    <div className={a.switchWrap}>
      <button
        ref={triggerRef}
        type="button"
        className={`${a.switch} ${open ? a.switchOpen : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`Conversations — ${currentTitle || "new conversation"}`}
        onClick={() => (open ? close(false) : show())}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            show();
          }
        }}
        data-testid="ask-switcher"
      >
        <History size={15} aria-hidden="true" />
        <span className={a.switchTitle}>{currentTitle || "New conversation"}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div
          ref={listRef}
          id={listId}
          className={a.menu}
          role="listbox"
          aria-label="Conversations"
          tabIndex={-1}
          onKeyDown={onKeyDown}
          data-modal-inside=""
          data-testid="ask-switcher-menu"
        >
          {rows.length === 0 ? (
            <div className={a.menuEmpty}>No conversations kept yet.</div>
          ) : (
            rows.map((c, i) => (
              <div
                key={c.id}
                role="option"
                aria-selected={c.id === currentId}
                tabIndex={i === active ? 0 : -1}
                className={`${a.menuItem} ${c.id === currentId ? a.menuCur : ""}`}
                onClick={() => {
                  close(true);
                  onPick(c.id);
                }}
                onFocus={() => setActiveId(c.id)}
                data-testid="ask-switcher-item"
              >
                <span className={a.menuTitle}>{c.title || "Untitled conversation"}</span>
                <span className={a.menuMeta}>{when(c.updated_at)}</span>
              </div>
            ))
          )}
          <div className={a.menuFoot} aria-hidden="true">
            Kept 30 days · last 50 conversations
          </div>
        </div>
      ) : null}
    </div>
  );
}
