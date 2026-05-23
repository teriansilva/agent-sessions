import { Link } from "react-router-dom";
import { useSessionsList } from "../../hooks/useSessionsList";
import { engineBadge, relTime, shortCwd } from "../../lib/format";
import type { Session } from "../../types/api";
import { FiltersBar } from "./Filters";
import styles from "./SessionList.module.css";

function Row({ s }: { s: Session }) {
  return (
    <li>
      <Link to={`/s/${s.engine}/${s.uuid}`} className={styles.row}>
        <span className={`${styles.badge} ${styles[s.engine] ?? ""}`}>{engineBadge(s.engine)}</span>
        <div className={styles.body}>
          <div className={styles.title}>{s.title || "(untitled)"}</div>
          <div className={styles.meta}>
            {shortCwd(s.cwd)} · {relTime(s.last_mtime)}
          </div>
        </div>
      </Link>
    </li>
  );
}

/** Sidebar: filters + facets + paginated session list. Rows link to the session
 *  URL (open/switch). Row actions (rename/archive) + new-session land in Phase 3b. */
export function SessionList() {
  const { sessions, total, facets, filters, loading, error, hasMore, loadMore, update, clear } =
    useSessionsList();

  return (
    <div className={styles.wrap}>
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
            <Row key={s.id} s={s} />
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
