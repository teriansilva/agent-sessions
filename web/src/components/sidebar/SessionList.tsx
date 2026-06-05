import { Archive, ArchiveRestore, Check, Pencil, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { useSessionsStore } from "../../app/sessionsStore";
import { useSessionsList } from "../../hooks/useSessionsList";
import { displayProjectName, engineBadge, relTime } from "../../lib/format";
import type { Session } from "../../types/api";
import { FiltersBar } from "./Filters";
import styles from "./SessionList.module.css";

interface RowProps {
  s: Session;
  onRename: (id: string, title: string) => Promise<void>;
  onToggleArchive: (id: string, currentlyArchived: boolean) => Promise<void>;
  /** Close the mobile drawer on tap — tapping the already-active row is a same-route no-op,
   *  so the route-change effect in App won't fire (#283). */
  onNavigate?: () => void;
}

function Row({ s, onRename, onToggleArchive, onNavigate }: RowProps) {
  const { projectNames } = useOverviewPrefs();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(s.title);
  const [busy, setBusy] = useState(false);

  const commit = async () => {
    const title = draft.trim();
    if (!title || title === s.title) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      await onRename(s.id, title);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  const toggleArchive = async () => {
    setBusy(true);
    try {
      await onToggleArchive(s.id, s.archived);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <li className={styles.rowWrap}>
        <form
          className={styles.editRow}
          onSubmit={(e) => {
            e.preventDefault();
            void commit();
          }}
        >
          <input
            ref={(el) => el?.focus()}
            className={styles.editInput}
            aria-label="Session title"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setDraft(s.title);
                setEditing(false);
              }
            }}
          />
          <button type="submit" className={styles.iconBtn} aria-label="Save title" disabled={busy}>
            <Check size={15} />
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            aria-label="Cancel rename"
            disabled={busy}
            onClick={() => {
              setDraft(s.title);
              setEditing(false);
            }}
          >
            <X size={15} />
          </button>
        </form>
      </li>
    );
  }

  return (
    <li className={styles.rowWrap}>
      <NavLink
        to={`/s/${s.engine}/${s.uuid}`}
        className={({ isActive }) => (isActive ? `${styles.row} ${styles.active}` : styles.row)}
        onClick={onNavigate}
      >
        {/* Leading status LED (#211 4b): a live row pulses green and carries the meaningful
            "agent working" status for screen readers; an idle row shows a dim, decorative dot.
            Reuses the global .hud-led primitive so the indicator matches the topbar/classbar. */}
        {s.working ? (
          <span
            className={`${styles.led} hud-led up`}
            role="status"
            aria-label="agent working"
            title="agent working"
          />
        ) : (
          <span className={`${styles.led} hud-led idle`} aria-hidden="true" title="idle" />
        )}
        <div className={styles.body}>
          <div className={styles.title}>{s.title || "(untitled)"}</div>
          <div className={styles.meta}>
            <span className={styles.engineTag}>{engineBadge(s.engine)}</span>
            <span className={styles.metaText}>
              {displayProjectName(s.cwd, projectNames)} · {relTime(s.last_mtime)}
            </span>
          </div>
        </div>
      </NavLink>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.iconBtn}
          aria-label="Rename session"
          disabled={busy}
          onClick={() => {
            setDraft(s.title);
            setEditing(true);
          }}
        >
          <Pencil size={15} />
        </button>
        <button
          type="button"
          className={styles.iconBtn}
          aria-label={s.archived ? "Unarchive session" : "Archive session"}
          disabled={busy}
          onClick={() => void toggleArchive()}
        >
          {s.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
        </button>
      </div>
    </li>
  );
}

interface SessionListProps {
  /** Close the mobile off-canvas drawer on tap. Threaded onto New session and the session
   *  rows because those can be same-route no-ops that the route-change effect misses (#283). */
  onNavigate?: () => void;
}

/** Sidebar: filters + facets + paginated session list. Rows link to the session
 *  URL (open/switch) and expose rename + archive/unarchive actions. */
export function SessionList({ onNavigate }: SessionListProps = {}) {
  const {
    sessions,
    total,
    facets,
    filters,
    loading,
    error,
    hasMore,
    loadMore,
    update,
    clear,
    renameRow,
    setArchived,
  } = useSessionsList();

  // Publish the loaded rows so the compact header can resolve the current session's title.
  const { setSessions } = useSessionsStore();
  useEffect(() => {
    setSessions(sessions);
  }, [sessions, setSessions]);

  // Relative-time labels otherwise stay frozen ("2m ago", "2m ago", …) until something else
  // re-renders the list. Bump a counter every ~30s while the tab is visible so `relTime` is
  // recomputed without forcing an API call (#159). Background poll handles row order.
  const [, setClockTick] = useState(0);
  useEffect(() => {
    if (typeof document === "undefined") return;
    let id: number | undefined;
    const start = () => {
      if (id == null) id = window.setInterval(() => setClockTick((t) => t + 1), 30_000);
    };
    const stop = () => {
      if (id != null) window.clearInterval(id);
      id = undefined;
    };
    const onVis = () => (document.hidden ? stop() : start());
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return (
    <div className={styles.wrap}>
      <Link to="/" className={`${styles.newBtn} shine`} onClick={onNavigate}>
        <Plus size={16} />
        New session
      </Link>
      <FiltersBar filters={filters} facets={facets} onChange={update} onClear={clear} />
      {error ? (
        <div className={styles.empty}>{error}</div>
      ) : sessions.length === 0 && !loading ? (
        <div className={styles.empty}>
          {filters.q || filters.project || filters.engine
            ? "No sessions match — clear filters."
            : filters.archived
              ? "No archived sessions."
              : "No sessions yet."}
        </div>
      ) : (
        <ul className={styles.list} aria-label={`${total} sessions`}>
          {sessions.map((s) => (
            <Row
              key={s.id}
              s={s}
              onRename={renameRow}
              onToggleArchive={setArchived}
              onNavigate={onNavigate}
            />
          ))}
          {hasMore && (
            <li className={styles.more}>
              <button type="button" onClick={loadMore} disabled={loading}>
                {loading ? "Loading…" : "Load more"}
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
