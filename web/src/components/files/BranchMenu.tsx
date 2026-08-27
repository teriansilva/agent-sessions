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
  const menuRef = useRef<HTMLDivElement>(null);

  // Clamped into the viewport: at 360px the panel is the whole screen and an un-clamped left
  // would push the menu off the right edge, which is exactly where the actions live. Computed
  // during render from the measurement the parent took — no effect, no cascading render.
  const width = Math.min(Math.max(rect.width, 200), window.innerWidth - 16);
  const pos = {
    top: Math.min(rect.bottom + 2, window.innerHeight - 120),
    left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
    width,
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
  }, [close, move, view]);

  // Focus the first item when the view changes, so a keyboard user is never left on <body>.
  useEffect(() => {
    const first = menuRef.current?.querySelector<HTMLElement>(
      "[data-menu-item]:not(:disabled)",
    );
    first?.focus();
  }, [view]);

  const deletable = local.filter((b) => b !== current);

  const body =
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
        {deletable.map((b) => (
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
            {b}
          </button>
        ))}
        <button
          type="button"
          role="menuitem"
          data-menu-item=""
          className={styles.headMenuItem}
          onClick={() => setView("list")}
        >
          Back
        </button>
      </>
    ) : (
      <>
        {local.map((b) => (
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
            <span className={styles.menuItemText}>{b}</span>
          </button>
        ))}
        {remote.length > 0 && (
          <>
            <div className={styles.menuHead}>
              <span className="hud-tag">Remote-tracking</span>
            </div>
            {remote.map((b) => (
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
                <span className={styles.menuItemText}>{b}</span>
              </button>
            ))}
          </>
        )}
        <div className={styles.menuSep} aria-hidden="true" />
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
    );

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
        className={`${styles.headMenu} ${styles.aboveSheet}`}
        role="menu"
        aria-label="Branches"
        data-branch-menu=""
        style={{ top: pos.top, left: pos.left, width: pos.width }}
      >
        {body}
      </div>
    </>,
    document.body,
  );
}
