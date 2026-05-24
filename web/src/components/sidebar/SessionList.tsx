import { Archive, ArchiveRestore, Check, Pencil, Plus, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useSessionsList } from "../../hooks/useSessionsList";
import { engineBadge, relTime, shortCwd } from "../../lib/format";
import type { Session } from "../../types/api";
import { FiltersBar } from "./Filters";
import styles from "./SessionList.module.css";

interface RowProps {
  s: Session;
  onRename: (id: string, title: string) => Promise<void>;
  onToggleArchive: (id: string, currentlyArchived: boolean) => Promise<void>;
}

function Row({ s, onRename, onToggleArchive }: RowProps) {
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
      <Link to={`/s/${s.engine}/${s.uuid}`} className={styles.row}>
        <span className={`${styles.badge} ${styles[s.engine] ?? ""}`}>{engineBadge(s.engine)}</span>
        <div className={styles.body}>
          <div className={styles.title}>{s.title || "(untitled)"}</div>
          <div className={styles.meta}>
            {shortCwd(s.cwd)} · {relTime(s.last_mtime)}
          </div>
        </div>
      </Link>
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

/** Sidebar: filters + facets + paginated session list. Rows link to the session
 *  URL (open/switch) and expose rename + archive/unarchive actions. */
export function SessionList() {
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

  return (
    <div className={styles.wrap}>
      <Link to="/" className={styles.newBtn}>
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
            <Row key={s.id} s={s} onRename={renameRow} onToggleArchive={setArchived} />
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
