/** The event log (#878).
 *
 * Pagination is by CURSOR (`events_next_seq`), never by page offset. That is not a performance
 * choice: events arrive while the operator reads, and an offset window shifts under them, so a
 * second page would duplicate rows or skip them. A cursor names a position in the log itself.
 */
import type { MissionEvent } from "../../types/api";

import styles from "./mission.module.css";

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function MissionTimeline({
  events,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  events: MissionEvent[];
  hasMore: boolean;
  loadingMore?: boolean;
  onLoadMore: () => void;
}) {
  if (events.length === 0) {
    return (
      <div className={styles.empty} data-testid="timeline-empty">
        Nothing has happened yet.
      </div>
    );
  }
  return (
    <div data-testid="timeline">
      <ol style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label="Timeline">
        {events.map((e) => (
          <li key={e.seq} className={styles.tlRow} data-testid="timeline-row">
            <span className={styles.tlAt}>{when(e.at)}</span>
            <span className={styles.tlKind}>{e.kind}</span>
            {/* Model-derived text, rendered as TEXT. React escapes it; there is no
                `dangerouslySetInnerHTML` anywhere in these components. */}
            <span className={styles.tlText}>{e.text ?? ""}</span>
          </li>
        ))}
      </ol>
      {hasMore ? (
        <button
          type="button"
          className={styles.more}
          onClick={onLoadMore}
          disabled={loadingMore}
          data-testid="timeline-more"
        >
          {loadingMore ? "Loading…" : "Load older"}
        </button>
      ) : null}
    </div>
  );
}
