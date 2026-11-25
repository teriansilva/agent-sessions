/** The top of an API session's log while scrolling up (#1504): each state in words, its status
 *  colour beside them, never colour alone. Nothing shows until a page has been asked for. */
import type { HistoryStatus } from "./useApiHistory";
import styles from "./StructuredPane.module.css";

export function HistoryTop({ status, onRetry }: { status: HistoryStatus; onRetry: () => void }) {
  if (status === "idle") return null;
  if (status === "error")
    return (
      <div className={styles.historyRow} data-state="error" data-history-row="" data-testid="api-history">
        <span className={`${styles.led} ${styles.ledDown}`} aria-hidden="true" />
        <span className={styles.historyFailed} role="alert">
          Couldn't load earlier turns.
        </span>
        <button
          type="button"
          className={styles.historyRetry}
          onClick={onRetry}
          data-testid="api-history-retry"
        >
          Retry
        </button>
      </div>
    );
  if (status === "end")
    return (
      <p className={styles.historyBegin} data-history-row="" data-testid="api-history">
        Beginning of conversation
      </p>
    );
  const loading = status === "loading";
  return (
    <p
      className={styles.historyRow}
      data-state={status}
      data-history-row=""
      role="status"
      data-testid="api-history"
    >
      <span
        className={`${styles.led} ${loading ? styles.historyLoadingLed : styles.ledIdle}`}
        aria-hidden="true"
      />
      <span>
        {loading
          ? "Loading earlier turns…"
          : "Older turns beyond what this pane keeps are not shown."}
      </span>
    </p>
  );
}
