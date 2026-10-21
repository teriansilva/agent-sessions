import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { RosterReload } from "../../app/reloadRoster";
import { SESSIONS_PATH } from "../../lib/routes";
import button from "../ui/actionButton.module.css";
import styles from "./RuntimeGate.module.css";

/** How long a session pane may say "Opening…" while it waits on the agent list before it says
 *  honestly that the list could not be loaded, with a Retry (#1502). No waiting state is open-ended. */
export const OPENING_GIVE_UP_MS = 8_000;

/** The neutral "Opening this session…" panel (#1502) — shown while the page cannot yet tell which
 *  pane a session needs (no runtime known), or while a native-id link is still resolving to its
 *  owner. It never says "not found": nothing has answered yet.
 *
 *  With `reload` (the roster reload), the wait is BOUNDED: after `OPENING_GIVE_UP_MS`, or as soon as
 *  a reload this panel ran reports `"failed"`, it turns into "Couldn't load the agent list" with a
 *  Retry that reloads the roster now. The panel never decides the pane itself — whoever rendered it
 *  re-renders the right pane when the roster lands, which unmounts this. `reloadOnMount` runs the
 *  reload at once (a terminal refused as "not a terminal session" asks for the roster now, not in
 *  15 s). `overlay` covers a mounted terminal's area. */
export function OpeningPanel({
  reload,
  reloadOnMount = false,
  overlay = false,
}: {
  reload?: () => Promise<RosterReload>;
  reloadOnMount?: boolean;
  overlay?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  // Bumped by Retry: each attempt gets its own timer and its own reload.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!reload) return;
    let alive = true;
    const timer = setTimeout(() => {
      if (alive) setFailed(true);
    }, OPENING_GIVE_UP_MS);
    if (reloadOnMount || attempt > 0) {
      void reload().then((outcome) => {
        if (alive && outcome === "failed") setFailed(true);
      });
    }
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [reload, reloadOnMount, attempt]);
  const retry = useCallback(() => {
    setFailed(false);
    setAttempt((n) => n + 1);
  }, []);

  const body = failed ? (
    <div className={`${styles.panel} ${styles.failed}`} role="alert" data-session-opening="failed">
      <div className={styles.tagLine}>
        <span className={styles.led} aria-hidden="true" />
        <p className={styles.tag}>SESSION VIEW // AGENT LIST UNAVAILABLE</p>
      </div>
      <p className={styles.message}>Couldn’t load the agent list.</p>
      <p className={styles.detail}>Nothing was started. BattleLab keeps trying in the background.</p>
      <button type="button" className={`${button.primary} ${styles.retry}`} onClick={retry}>
        Retry
      </button>
    </div>
  ) : (
    <div className={styles.panel} role="status" data-session-opening="opening">
      <p className={styles.tag}>SESSION VIEW // OPENING</p>
      <p className={styles.line}>
        <span className={styles.pips} aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        Opening this session…
      </p>
    </div>
  );
  return overlay ? <div className={styles.overlay}>{body}</div> : body;
}

/** The SETTLED not-found result (#1502): shown only once the session lookup has answered 404 —
 *  never while anything is still being looked up. Neutral, not an alarm: a link to a session that
 *  no longer exists is not a failure of BattleLab. */
export function NotFoundPanel({ overlay = false }: { overlay?: boolean }) {
  const body = (
    <div className={styles.panel} role="status" data-session-opening="not-found">
      <p className={styles.tag}>SESSION VIEW // NOT FOUND</p>
      <p>This session was not found.</p>
      <Link className={styles.back} to={SESSIONS_PATH}>
        ← Back to Sessions
      </Link>
    </div>
  );
  return overlay ? <div className={styles.overlay}>{body}</div> : body;
}
