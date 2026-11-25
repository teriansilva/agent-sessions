/** The conversation switcher in Ask's head (#1443). It takes the head's spare room and shows the
 *  current conversation's title, truncated; it opens a list of the kept conversations.
 *
 *  **The list.** It drops from the head's bottom edge across the WHOLE head, not just under the
 *  switcher: beside a portrait and five buttons the switcher can be a thumb wide, and a list that
 *  narrow showed "Hey, …" and nothing else. A search field filters it by title (every word, any
 *  order, accents folded). Conversations with an unseen update are pinned on top under "Updates"
 *  and say so in words; the rest sit under day headings, latest first (`askSwitcherList`). The
 *  closed switcher carries a corner marker when ANOTHER conversation has an update (the count is
 *  in its accessible name), so it is visible without opening the list.
 *
 *  **Keyboard.** Opening moves focus INTO the list, onto the current conversation (or the first),
 *  never into the search field — on a phone that would raise the keyboard over the list. Arrow
 *  keys, Home and End move between rows; Enter or Space opens one. Typing while a row has focus
 *  goes to the search field; ArrowUp from the first row reaches it, and ArrowDown or Tab from it
 *  goes back to the rows; Enter in it opens the first match. Escape closes the list and hands
 *  focus back to the switcher, and Tab away from the rows closes it too. Escape is answered here
 *  and goes no further: on a desktop the panel's own Escape would close Ask, and in the phone
 *  drawer the list declares `data-modal-inside`, which is what the drawer's Escape stands down for.
 *
 *  A click outside closes it. It is not portalled: the sidebar already portals itself to `<body>`
 *  above the top bar's stacking context, so the list can sit inside the panel.
 */
import { ChevronDown, History, Search } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { AskConversationSummary } from "../../types/api";

import { GROUP_LABEL, arrange, stamp } from "./askSwitcherList";
import a from "./AskConsole.module.css";

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
  const [query, setQuery] = useState("");
  /** Where focus belongs while open: on the rows (roving) or in the search field. */
  const [zone, setZone] = useState<"rows" | "search">("rows");
  /** The active row by conversation ID, never by index: a refresh may reorder the rows while the
   *  list is open, and Enter must open the row that has focus. */
  const [activeId, setActiveId] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const all = useMemo(() => conversations ?? [], [conversations]);
  const listId = useId();
  const groupId = useId();
  const sections = useMemo(() => arrange(all, query, new Date()), [all, query]);
  const rows = useMemo(() => sections.flatMap((s) => s.rows), [sections]);
  const found = rows.findIndex((c) => c.id === activeId);
  const active = found >= 0 ? found : 0;
  const order = rows.map((c) => c.id).join(" ");
  const updates = all.filter((c) => c.unread && c.id !== currentId).length;
  const now = new Date();

  const moveTo = (i: number) => {
    const row = rows[Math.max(0, Math.min(rows.length - 1, i))];
    if (row) setActiveId(row.id);
    setZone("rows");
  };

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  };

  const pick = (id: string) => {
    close(true);
    onPick(id);
  };

  const show = () => {
    onOpen();
    setQuery("");
    setZone("rows");
    setActiveId(all.some((c) => c.id === currentId) ? currentId : null);
    setOpen(true);
  };

  // Focus follows the active row while the rows hold it — a roving tabindex. Re-run on a reorder
  // too (`order`), so the focused node is always the active conversation's. While the search field
  // holds focus, a narrowing list never pulls it away.
  useEffect(() => {
    if (!open) return;
    if (zone === "search") {
      searchRef.current?.focus();
      return;
    }
    const items = listRef.current?.querySelectorAll<HTMLElement>('[role="option"]');
    if (items && items.length) items[Math.min(active, items.length - 1)]?.focus();
    else listRef.current?.focus();
  }, [open, zone, active, order]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || triggerRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onSearchKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
      if (!rows.length) return;
      e.preventDefault();
      moveTo(0);
    } else if (e.key === "Tab") {
      close(false);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (rows[0]) pick(rows[0].id);
    }
  };

  const onRowsKey = (e: React.KeyboardEvent) => {
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
      if (active <= 0) setZone("search");
      else moveTo(active - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      moveTo(0);
    } else if (e.key === "End") {
      e.preventDefault();
      moveTo(last);
    } else if ((e.key === "Enter" || e.key === " ") && rows[active]) {
      e.preventDefault();
      pick(rows[active].id);
    } else if (e.key === "Backspace") {
      e.preventDefault();
      setQuery((q) => q.slice(0, -1));
      setZone("search");
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Type to search: the key lands in the search field, which takes focus.
      e.preventDefault();
      setQuery((q) => q + e.key);
      setZone("search");
    }
  };

  const triggerLabel =
    `Conversations — ${currentTitle || "new conversation"}` +
    (updates ? ` — ${updates} with new updates` : "");

  return (
    <div className={a.switchWrap}>
      <button
        ref={triggerRef}
        type="button"
        className={`${a.switch} ${open ? a.switchOpen : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={triggerLabel}
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
        {updates ? (
          // A corner marker, not inline: beside a portrait the switcher is a thumb wide and a
          // count there pushed the title out. The count is in the name, and on "Updates · N".
          <span className={a.switchUpdates} aria-hidden="true" data-testid="ask-switcher-updates" />
        ) : null}
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open ? (
        <div
          ref={menuRef}
          className={a.menu}
          data-modal-inside=""
          data-testid="ask-switcher-menu"
        >
          <label className={a.menuSearch}>
            <Search size={15} aria-hidden="true" />
            <input
              ref={searchRef}
              type="search"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActiveId(null);
              }}
              onFocus={() => setZone("search")}
              onKeyDown={onSearchKey}
              placeholder="Search conversations"
              aria-label="Search conversations"
              aria-controls={listId}
              autoComplete="off"
              spellCheck={false}
              data-testid="ask-switcher-search"
            />
          </label>
          <div
            ref={listRef}
            id={listId}
            className={a.menuList}
            role="listbox"
            aria-label="Conversations"
            tabIndex={-1}
            onKeyDown={onRowsKey}
          >
            {all.length === 0 ? (
              <div className={a.menuEmpty}>No conversations kept yet.</div>
            ) : rows.length === 0 ? (
              <div className={a.menuEmpty} data-testid="ask-switcher-search-empty">
                No conversation matches “{query.trim()}”
              </div>
            ) : (
              sections.map((s) => (
                <div
                  key={s.group}
                  role="group"
                  aria-labelledby={`${groupId}-${s.group}`}
                  data-group={s.group}
                >
                  <div
                    id={`${groupId}-${s.group}`}
                    role="presentation"
                    className={`${a.menuGroup} ${s.group === "updates" ? a.menuGroupUpdates : ""}`}
                    data-testid="ask-switcher-group"
                  >
                    {GROUP_LABEL[s.group]}
                    {s.group === "updates" ? ` · ${s.rows.length}` : ""}
                  </div>
                  {s.rows.map((c) => {
                    const i = rows.indexOf(c);
                    const cur = c.id === currentId;
                    return (
                      <div
                        key={c.id}
                        role="option"
                        aria-selected={cur}
                        tabIndex={zone === "rows" && i === active ? 0 : -1}
                        className={`${a.menuItem} ${cur ? a.menuCur : ""} ${
                          c.unread ? a.menuItemUnread : ""
                        }`}
                        onClick={() => pick(c.id)}
                        onFocus={() => {
                          setActiveId(c.id);
                          setZone("rows");
                        }}
                        data-testid="ask-switcher-item"
                      >
                        <span className={a.menuTitle}>{c.title || "Untitled conversation"}</span>
                        <span className={a.menuMeta}>
                          {c.unread ? (
                            <span className={a.menuUnread} data-testid="ask-switcher-unread">
                              <span className={a.menuUnreadDot} aria-hidden="true" />
                              New updates
                            </span>
                          ) : null}
                          {cur ? <span className={a.menuNow}>On screen</span> : null}
                          <span className={a.menuTime}>{stamp(c.updated_at, s.group, now)}</span>
                        </span>
                      </div>
                    );
                  })}
                </div>
              ))
            )}
          </div>
          <div className={a.menuFoot} aria-hidden="true">
            {query.trim() && all.length
              ? `${rows.length} of ${all.length} · kept 30 days · last 50`
              : "Kept 30 days · last 50 conversations"}
          </div>
        </div>
      ) : null}
    </div>
  );
}
