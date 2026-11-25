/** One notification row (#2146): the bell's row, shared with HQ's Notifications view so both say
 *  the same facts with the same actions — title, ✕ dismiss, reason, project, agent, age, Open.
 *
 *  The view adds the class word over the title (`kind`) and the long Open label, and draws a member
 *  the bell no longer holds from its dispatch snapshot (`gone`): no read state, no ✕, its own Open
 *  intact, stamped "No longer in the bell". */
import { ArrowRight, X } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { engineBadge, relTime } from "../../lib/format";
import styles from "./NotificationBell.module.css";

export function NotificationRow({
  title,
  reason,
  project,
  engine,
  ts,
  unread = false,
  arrived = false,
  member = false,
  gone = false,
  kind,
  open,
  openText = "Open",
  onOpen,
  onDismiss,
  testId,
  extra,
}: {
  title: string;
  reason?: string;
  project?: string;
  engine?: string;
  ts: number;
  unread?: boolean;
  arrived?: boolean;
  /** One of the rows an HQ line covers: the quiet rail. */
  member?: boolean;
  /** Drawn from a dispatch's snapshot: the bell no longer holds it. */
  gone?: boolean;
  /** The class word over the title (the view). */
  kind?: ReactNode;
  /** The row's Open target; "" draws no Open. */
  open: string;
  openText?: string;
  onOpen?: () => void;
  /** Absent: no ✕ (a snapshot cannot be dismissed — it is not a bell row). */
  onDismiss?: () => void;
  testId?: string;
  /** Under the reason, above the foot (the bell's single-row HQ line). */
  extra?: ReactNode;
}) {
  const cls = [
    styles.row,
    unread ? styles.unread : "",
    arrived ? styles.arrived : "",
    member ? styles.member : "",
    gone ? styles.gone : "",
    kind ? styles.withKind : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <li
      className={cls}
      data-testid={testId}
      data-unread={unread ? "true" : undefined}
    >
      {kind ? <span className={styles.kind}>{kind}</span> : null}
      {onDismiss ? (
        <button
          type="button"
          className={styles.rowX}
          onClick={onDismiss}
          aria-label={`Dismiss: ${title}`}
        >
          <X size={16} aria-hidden="true" />
        </button>
      ) : null}
      <div className={styles.title}>{title}</div>
      {reason ? <div className={styles.reason}>{reason}</div> : null}
      {extra}
      <div className={styles.foot}>
        {project ? <span className={styles.proj}>{project}</span> : null}
        {engine ? (
          <span className={styles.eng} aria-hidden="true">
            {engineBadge(engine)}
          </span>
        ) : null}
        {ts > 0 ? <span className={styles.age}>{relTime(ts)}</span> : null}
        {gone ? (
          <span
            className={`nj-stamp ${styles.goneStamp}`}
            data-testid="notification-gone"
          >
            No longer in the bell
          </span>
        ) : null}
        {open ? (
          <Link className={styles.open} to={open} onClick={onOpen}>
            {openText}
            <ArrowRight size={15} aria-hidden="true" />
          </Link>
        ) : null}
      </div>
    </li>
  );
}
