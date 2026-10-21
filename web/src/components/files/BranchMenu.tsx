import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, GitBranch, Plus, Trash2 } from "lucide-react";
import styles from "./filePanel.module.css";

type View = "list" | "create" | "delete";

/** The branch menu (#806).
 *
 *  Portalled to <body> for the reason the head overflow menu already is: `.terminal-pane` is
 *  `overflow: hidden`, so an in-tree popover is clipped away at the panel edge.
 *
 *  Three views behind one trigger rather than three separate controls in the strip — the strip is
 *  ~200px wide on a docked panel and there is no room for a row of buttons. The destructive-ish
 *  one (delete) sorts **last**, where a mis-tap is least likely, and it is not actually
 *  destructive: the server runs `git branch -d`, which refuses an unmerged branch outright.
 */
/** The viewport the menu has to fit inside.
 *
 *  `window.innerHeight` does not move when an on-screen keyboard opens — `visualViewport` does,
 *  and on a phone the keyboard is exactly what appears underneath an open menu. Falls back to the
 *  window where `visualViewport` is absent. */
function viewport() {
  const vv = typeof window === "undefined" ? null : window.visualViewport;
  return { width: vv?.width ?? window.innerWidth, height: vv?.height ?? window.innerHeight };
}

/** The branch name with the matched run marked, so a filtered list says WHY each row is there. */
function marked(name: string, q: string) {
  if (!q) return name;
  const at = name.toLowerCase().indexOf(q);
  if (at < 0) return name;
  return (
    <>
      {name.slice(0, at)}
      <mark className={styles.menuHit}>{name.slice(at, at + q.length)}</mark>
      {name.slice(at + q.length)}
    </>
  );
}

export function BranchMenu({
  current,
  local,
  remote,
  busy,
  onSwitch,
  onCreate,
  onDelete,
  onClose,
  anchor,
  rect,
}: {
  current: string | null;
  local: string[];
  remote: string[];
  busy: boolean;
  onSwitch: (branch: string) => void;
  onCreate: (branch: string, from?: string) => void;
  onDelete: (branch: string) => void;
  onClose: () => void;
  /** The control that opened the menu; focus goes back to it on every close path. */
  anchor: HTMLElement | null;
  /** The trigger's geometry, measured by the PARENT at click time.
   *
   *  Deliberately a prop rather than a `getBoundingClientRect()` in a layout effect here: reading
   *  it into state after mount is a synchronous setState inside an effect, which cascades a
   *  render — and reading `anchor.current` during render is its own violation. The parent already
   *  holds the event that opened this, so the measurement is free and the position is pure. */
  rect: { top: number; bottom: number; left: number; width: number };
}) {
  const [view, setView] = useState<View>("list");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  const [vp, setVp] = useState(viewport);

  // Re-place when the viewport moves under an OPEN menu: a keyboard opening, a rotation, a window
  // resize. Without this the menu keeps the geometry it was placed with and the bottom edge ends
  // up outside whatever is left of the screen.
  useEffect(() => {
    const onChange = () => setVp(viewport());
    const vv = window.visualViewport;
    vv?.addEventListener("resize", onChange);
    vv?.addEventListener("scroll", onChange);
    window.addEventListener("resize", onChange);
    return () => {
      vv?.removeEventListener("resize", onChange);
      vv?.removeEventListener("scroll", onChange);
      window.removeEventListener("resize", onChange);
    };
  }, []);

  // Clamped into the viewport: at 360px the panel is the whole screen and an un-clamped left
  // would push the menu off the right edge, which is exactly where the actions live. Computed
  // during render from the measurement the parent took — no effect, no cascading render.
  //
  // Containment outranks the minimum height (#1005). A floor may move the menu UP; it may never
  // push the bottom edge past the viewport — a `Math.max(160, …)` cap under a `top` that is itself
  // clamped to `height - 120` ends 40px off the bottom of a 360px-tall window, which is the bug
  // this menu is being fixed for, reintroduced by its own fix.
  const EDGE = 8;
  // The pinned header and footer are spent before a single row can show, so a cap smaller than
  // them buys nothing: the box sits inside the viewport while `overflow: hidden` clips the rows
  // and the actions away — the original bug in another shape (Hermes on #1008). Measured on a
  // coarse pointer: header 77px, footer 97px, a row 44px. A side must hold all three to be worth
  // choosing.
  const CHROME = 174;
  const ROW = 44;
  const MIN_USABLE = CHROME + ROW;
  const width = Math.min(Math.max(rect.width, 200), vp.width - 2 * EDGE);
  const below = vp.height - (rect.bottom + 2) - EDGE;
  const above = rect.top - 2 * EDGE;
  const room = Math.max(0, vp.height - 2 * EDGE);
  let top: number;
  let maxHeight: number;
  if (Math.max(below, above) >= MIN_USABLE) {
    // A side that can seat the chrome and a row: prefer below, flip above when it is roomier.
    if (below >= above) {
      top = Math.max(EDGE, rect.bottom + 2);
      maxHeight = vp.height - top - EDGE;
    } else {
      top = EDGE;
      maxHeight = rect.top - EDGE - top;
    }
  } else {
    // Neither side can. Stop pinning to the trigger and take the whole visual viewport — an
    // overlaid menu the operator can actually use beats a tidily-placed one they cannot.
    top = EDGE;
    maxHeight = room;
  }
  const pos = {
    top,
    left: Math.max(EDGE, Math.min(rect.left, vp.width - width - EDGE)),
    width,
    // Never taller than the viewport itself, whichever branch chose it.
    maxHeight: Math.max(0, Math.min(maxHeight, room)),
  };

  const close = useCallback(() => {
    onClose();
    if (anchor && document.contains(anchor)) anchor.focus();
  }, [onClose, anchor]);

  // Roving focus over whatever the current view renders. Queried from the DOM rather than kept
  // in state because the three views have different item counts and a stale index would land
  // focus on nothing.
  const move = useCallback((delta: number) => {
    // `:not(:disabled)` is load-bearing, not tidiness: the FIRST item is the current branch,
    // which is disabled, and `.focus()` on a disabled control silently does nothing — so both
    // the initial focus and every ArrowDown landed on <body> and the menu was unusable by
    // keyboard. Caught by the e2e; no jsdom assertion would have noticed.
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
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        // One step at a time, and a typed filter is the FIRST step: clearing it before backing
        // out of the delete view means Escape never throws away two things at once.
        if (query) {
          setQuery("");
          return;
        }
        // A sub-view backs out to the list first: Escape should undo one step, not throw away
        // a half-typed branch name AND close the menu in a single press.
        if (view !== "list") {
          setView("list");
          setName("");
          return;
        }
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
  }, [close, move, view, query]);

  // Focus the first item when the view changes, so a keyboard user is never left on <body>.
  useEffect(() => {
    const first = menuRef.current?.querySelector<HTMLElement>(
      "[data-menu-item]:not(:disabled)",
    );
    first?.focus();
  }, [view]);

  const deletable = local.filter((b) => b !== current);

  // Filtering is a render-time derivation, not state: the query is the only thing stored and the
  // three lists fall out of it during render — no effect, no cascading render, the same discipline
  // the placement already follows. Filtered-out rows leave the DOM entirely, which is what lets the
  // roving-focus query keep walking exactly what is on screen.
  const q = query.trim().toLowerCase();
  const hits = (b: string) => b.toLowerCase().includes(q);
  const shownLocal = q ? local.filter(hits) : local;
  const shownRemote = q ? remote.filter(hits) : remote;
  const shownDeletable = q ? deletable.filter(hits) : deletable;

  const total = view === "delete" ? deletable.length : local.length + remote.length;
  const shown =
    view === "delete" ? shownDeletable.length : shownLocal.length + shownRemote.length;
  // The count is what answers "is my branch hidden, or not there at all?" — so it always names
  // the total it was filtered from.
  const countText = q
    ? `${shown} match // ${total} total`
    : view === "delete"
      ? `${deletable.length} deletable`
      : `${local.length} local // ${remote.length} remote`;

  /** Enter in the filter moves FOCUS to the first match. Deliberately never a checkout: a switch
   *  touches the working tree, and one keystroke from a typed filter is far too easy to fire by
   *  accident. With no matches there is nothing to focus and the press must not fall through to a
   *  pinned action either, so it is swallowed. */
  const onFilterKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    menuRef.current
      ?.querySelector<HTMLElement>(
        "[data-branch]:not(:disabled), [data-branch-remote]:not(:disabled), [data-branch-delete]:not(:disabled)",
      )
      ?.focus();
  };

  const header =
    view === "create" ? null : (
      <div className={styles.menuTop} data-branch-header="">
        <input
          type="search"
          data-menu-item=""
          data-branch-filter=""
          className={styles.menuSearch}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onFilterKey}
          placeholder="Filter branches…"
          aria-label="Filter branches"
          autoComplete="off"
          spellCheck={false}
        />
        <span className={`hud-tag ${styles.menuCount}`} data-branch-count="">
          {countText}
        </span>
      </div>
    );

  const list =
    view === "create" ? (
      <form
        className={styles.branchForm}
        onSubmit={(e) => {
          e.preventDefault();
          const v = name.trim();
          if (v) onCreate(v);
        }}
      >
        <label className={styles.branchFormLabel} htmlFor="git-new-branch">
          New branch from {current ?? "HEAD"}
        </label>
        <input
          id="git-new-branch"
          data-menu-item=""
          className={styles.branchInput}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="devopsagent/my-branch"
          autoComplete="off"
          spellCheck={false}
        />
        <div className={styles.branchFormRow}>
          <button
            type="button"
            data-menu-item=""
            className={styles.ctrlBtn}
            onClick={() => {
              setView("list");
              setName("");
            }}
          >
            Cancel
          </button>
          <button
            type="submit"
            data-menu-item=""
            className={`${styles.ctrlBtn} ${styles.ctrlPrimary}`}
            disabled={!name.trim() || busy}
          >
            Create
          </button>
        </div>
      </form>
    ) : view === "delete" ? (
      <>
        <div className={styles.menuHead}>
          <span className="hud-tag">Delete a local branch</span>
        </div>
        {deletable.length === 0 && (
          <div className={styles.menuEmpty}>
            There is no other local branch to delete.
          </div>
        )}
        {deletable.length > 0 && shownDeletable.length === 0 && (
          <div className={styles.menuEmpty} data-branch-empty="">
            No branch matches “{query}”. Clear the filter to see all {deletable.length}.
          </div>
        )}
        {shownDeletable.map((b) => (
          <button
            key={b}
            type="button"
            role="menuitem"
            data-menu-item=""
            data-branch-delete={b}
            className={styles.headMenuItem}
            disabled={busy}
            // `-d`, never `-D`: an unmerged branch comes back as a refusal that says so, so this
            // cannot lose commits and does not ask for a confirmation it would not honour.
            title={`Delete ${b} (refused if it is not fully merged)`}
            onClick={() => onDelete(b)}
          >
            <Trash2 size={12} aria-hidden="true" />
            <span className={styles.menuItemText}>{marked(b, q)}</span>
          </button>
        ))}
      </>
    ) : (
      <>
        {shownLocal.map((b) => (
          <button
            key={b}
            type="button"
            role="menuitem"
            data-menu-item=""
            data-branch={b}
            className={styles.headMenuItem}
            disabled={busy || b === current}
            aria-current={b === current ? "true" : undefined}
            title={b === current ? `${b} (current)` : `Switch to ${b}`}
            onClick={() => onSwitch(b)}
          >
            {b === current ? (
              <Check size={12} aria-hidden="true" />
            ) : (
              <GitBranch size={12} aria-hidden="true" />
            )}
            <span className={styles.menuItemText}>{marked(b, q)}</span>
          </button>
        ))}
        {shownRemote.length > 0 && (
          <>
            <div className={styles.menuHead}>
              <span className="hud-tag">Remote-tracking</span>
            </div>
            {shownRemote.map((b) => (
              <button
                key={b}
                type="button"
                role="menuitem"
                data-menu-item=""
                data-branch-remote={b}
                className={styles.headMenuItem}
                disabled={busy}
                // Checking out a remote-tracking ref by its own name would detach HEAD, so this
                // creates the local branch that tracks it instead — which is what the operator
                // meant by clicking `origin/x`.
                title={`Create a local branch from ${b}`}
                onClick={() => onCreate(b.split("/").slice(1).join("/") || b, b)}
              >
                <GitBranch size={12} aria-hidden="true" />
                <span className={styles.menuItemText}>{marked(b, q)}</span>
              </button>
            ))}
          </>
        )}
        {q && shownLocal.length === 0 && shownRemote.length === 0 && (
          <div className={styles.menuEmpty} data-branch-empty="">
            No branch matches “{query}”. Clear the filter to see all {total} branches.
          </div>
        )}
      </>
    );

  // Pinned BELOW the scrolling list rather than sitting at the end of it. At 34 branches the
  // actions were 34 rows past the fold with no way to reach them, which is the complaint this
  // menu is being fixed for. The delete view keeps its own back-out control here and gains no
  // second delete launcher — deleting is the view it is already in.
  const foot =
    view === "delete" ? (
      <button
        type="button"
        role="menuitem"
        data-menu-item=""
        data-menu-back=""
        className={styles.headMenuItem}
        onClick={() => setView("list")}
      >
        Back
      </button>
    ) : view === "list" ? (
      <>
        <button
          type="button"
          role="menuitem"
          data-menu-item=""
          className={styles.headMenuItem}
          disabled={busy}
          onClick={() => setView("create")}
        >
          <Plus size={12} aria-hidden="true" />
          New branch…
        </button>
        <button
          type="button"
          role="menuitem"
          data-menu-item=""
          className={styles.headMenuItem}
          disabled={busy || deletable.length === 0}
          onClick={() => setView("delete")}
        >
          <Trash2 size={12} aria-hidden="true" />
          Delete branch…
        </button>
      </>
    ) : null;

  return createPortal(
    <>
      <button
        type="button"
        className={styles.menuScrim}
        aria-label="Close the branch menu"
        onClick={close}
      />
      <div
        ref={menuRef}
        className={`${styles.headMenu} ${styles.branchMenu} ${styles.aboveSheet}`}
        role="menu"
        aria-label="Branches"
        data-branch-menu=""
        style={{ top: pos.top, left: pos.left, width: pos.width, maxHeight: pos.maxHeight }}
      >
        {header}
        <div className={styles.menuList} data-branch-list="">
          {list}
        </div>
        {foot && (
          <div className={styles.menuFoot} data-branch-foot="">
            {foot}
          </div>
        )}
      </div>
    </>,
    document.body,
  );
}
