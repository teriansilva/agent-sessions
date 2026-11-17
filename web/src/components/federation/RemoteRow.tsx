import { NavLink } from "react-router-dom";
import { remotePath, remoteRowState, type RemoteShare } from "../../lib/federation";
import { PeerBadge, PeerGlyph } from "./PeerBadge";
import styles from "./Remote.module.css";

/** A session another instance shares, in the session list (#2055).
 *
 *  It is a separate component on purpose, and it takes a `RemoteShare`, never a `SessionEntry`:
 *  no local filter, page, selection, menu or bulk action can reach it. It shows what the list
 *  read carries and nothing else: the title, the peer's badge, "view only" and the end date, or
 *  "offline" while the peer's channel is down. No LED (this instance does not know whether that
 *  agent is working), no summary, no ⋯. It links to the read-only pane's own route. */
export function RemoteRow({
  remote,
  onNavigate,
}: {
  remote: RemoteShare;
  onNavigate?: () => void;
}) {
  const row = remoteRowState(remote);
  return (
    <li className={styles.rowWrap} data-testid="remote-row" data-offline={row.offline || undefined}>
      <NavLink
        to={remotePath(remote.peer_id, remote.grant_id)}
        className={({ isActive }) =>
          [styles.row, isActive ? styles.rowActive : "", row.offline ? styles.rowOffline : ""]
            .filter(Boolean)
            .join(" ")
        }
        aria-label={row.label}
        onClick={onNavigate}
      >
        <span className={styles.origin} aria-hidden="true">
          <PeerGlyph size={13} />
        </span>
        <span className={styles.rowBody}>
          <span className={styles.rowTitle}>{row.title}</span>
          <span className={styles.rowMeta}>
            <PeerBadge name={row.peer} />
            <span data-testid="remote-row-meta">{row.meta}</span>
          </span>
        </span>
      </NavLink>
    </li>
  );
}

/** The remote rows of the session list, after the local ones. Renders nothing when no peer
 *  shares anything. */
export function RemoteRows({
  remote,
  onNavigate,
}: {
  remote: readonly RemoteShare[];
  onNavigate?: () => void;
}) {
  return (
    <>
      {remote.map((r) => (
        <RemoteRow key={`${r.peer_id}/${r.grant_id}`} remote={r} onNavigate={onNavigate} />
      ))}
    </>
  );
}
