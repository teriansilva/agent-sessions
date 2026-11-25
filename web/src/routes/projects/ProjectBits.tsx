/** Small pieces HQ → Projects (#2143) draws on more than one page. */
import { RotateCw } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";
import type { ProjectArchiveReport } from "../../types/api";
import action from "../../components/ui/actionButton.module.css";
import styles from "./projects.module.css";

/** A project's colour: the stored hex is DATA (the operator's choice), set as `--pc`. No colour is
 *  a dashed empty square, never a guessed one. */
export function ProjectSwatch({ color, large = false }: { color: string; large?: boolean }) {
  return (
    <span
      className={`${styles.swatch} ${large ? styles.swatchLg : ""} ${color ? "" : styles.swatchNone}`}
      style={color ? ({ "--pc": color } as CSSProperties) : undefined}
      aria-hidden="true"
    />
  );
}

/** A tab label with a short form on a phone (General · Defaults · Playbook · Memory). The full
 *  name stays the accessible name: the short one is `aria-hidden`, the long one only clipped. */
export function TabLabel({ long, short, count }: { long: string; short?: string; count?: number }) {
  return (
    <>
      {short ? (
        <>
          <span className={styles.long}>{long}</span>
          <span className={styles.short} aria-hidden="true">
            {short}
          </span>
        </>
      ) : (
        long
      )}
      {count !== undefined && <span className={styles.tabN}>{count}</span>}
    </>
  );
}

/** A read that failed with nothing to show: the down tone (design.md §3), in place, with Retry. */
export function ReadFailed({
  title,
  detail,
  onRetry,
  testId,
}: {
  title: string;
  detail: ReactNode;
  onRetry: () => void;
  testId?: string;
}) {
  return (
    <div className={styles.failed} role="alert" data-testid={testId}>
      <p className={styles.failedTitle}>{title}</p>
      <p className={styles.failedText}>{detail}</p>
      <button type="button" className={`${action.ghost} ${action.compact}`} onClick={onRetry}>
        <RotateCw size={13} aria-hidden="true" /> Retry
      </button>
    </div>
  );
}

/** A read that failed while the last good data is still shown: degraded (amber), with Retry. */
export function StaleNotice({
  children,
  onRetry,
  testId,
}: {
  children: ReactNode;
  onRetry: () => void;
  testId?: string;
}) {
  return (
    <div className={styles.stale} role="status" data-testid={testId}>
      <p className={styles.failedText}>{children}</p>
      <button type="button" className={`${action.ghost} ${action.compact}`} onClick={onRetry}>
        <RotateCw size={13} aria-hidden="true" /> Retry
      </button>
    </div>
  );
}

/** "1 archived · 2 already archived · 1 failed" from the bulk report's counts — the keys mirror
 *  the direction, so the same formatter serves both endpoints. */
function formatCounts(counts: Record<string, number>): string {
  return Object.entries(counts)
    .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
    .join(" · ");
}

/** The archive / unarchive report (#361 Phase 2): the per-member counts, the failed members and a
 *  blind Retry of the SAME endpoint (idempotent: the done set reports `already_*`). */
export function BulkReport({
  report,
  onRetry,
  onDismiss,
}: {
  report: ProjectArchiveReport;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const failed = report.sessions.filter((s) => s.result === "failed");
  return (
    <div className={failed.length ? styles.stale : styles.report} role="status">
      <p className={styles.failedText}>
        {report.archived ? "Archive" : "Unarchive"}: {formatCounts(report.counts)}
      </p>
      {failed.length > 0 && (
        <ul className={styles.failedList} aria-label="Failed sessions">
          {failed.map((s) => (
            <li key={s.id}>
              <code>{s.id}</code> — {s.reason || "failed"}
            </li>
          ))}
        </ul>
      )}
      <div className={styles.actions}>
        {failed.length > 0 && (
          <button type="button" className={`${action.ghost} ${action.compact}`} onClick={onRetry}>
            <RotateCw size={13} aria-hidden="true" /> Retry
          </button>
        )}
        <button type="button" className={`${action.ghost} ${action.compact}`} onClick={onDismiss}>
          Dismiss
        </button>
      </div>
    </div>
  );
}
