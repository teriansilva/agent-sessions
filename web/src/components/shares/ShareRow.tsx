import {
  createdLabel,
  expiryChip,
  modeChip,
  openedLabel,
  type PublicShare,
} from "../../lib/shareFormat";
import styles from "./Shares.module.css";

/** One live public link (#1404 P4), in the dialog's per-session list and in Settings → Sharing.
 *  Never the URL: the server returns that once, at creation. A link with no password is flagged,
 *  and so is a send link — the operator may make either ("label risk, don't police"). */
export function ShareRow({
  share,
  now,
  busy,
  onRevoke,
  showTitle = false,
}: {
  share: PublicShare;
  now: number;
  busy: boolean;
  onRevoke: () => void;
  /** Settings lists every session's links, so each row names its session. */
  showTitle?: boolean;
}) {
  return (
    <li className={styles.row} data-share-row={share.share_id}>
      <div className={styles.rowMain}>
        {showTitle && <span className={styles.rowTitle}>{share.title || share.session_key}</span>}
        <span className={styles.chips}>
          <span className={`${styles.chip} ${styles.chipMode}`}>{modeChip(share.mode)}</span>
          {share.kind === "terminal" && <span className={styles.chip}>TERMINAL</span>}
          <span className={share.has_password ? styles.chip : `${styles.chip} ${styles.chipWarn}`}>
            {share.has_password ? "PASSWORD" : "NO PASSWORD"}
          </span>
          <span className={styles.chip}>{expiryChip(share.expires_at, now)}</span>
        </span>
        <span className={styles.rowMeta}>
          {createdLabel(share.created_at)} · {openedLabel(share, now)}
        </span>
      </div>
      <button
        type="button"
        className={styles.revoke}
        disabled={busy}
        aria-label={`Revoke ${modeChip(share.mode).toLowerCase()} link${showTitle && share.title ? ` to ${share.title}` : ""}`}
        onClick={onRevoke}
      >
        {busy ? "Revoking…" : "Revoke"}
      </button>
    </li>
  );
}
