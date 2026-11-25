import { X } from "lucide-react";
import { useEffect } from "react";
import styles from "./MapFullNotice.module.css";

/** How long the notice stays before it goes by itself. */
export const MAP_FULL_NOTICE_MS = 8000;

/** "The map was full — opened full screen" (#1648), on the session page the map's drain handed a
 *  refused window back to. The map has unmounted by then, so the notice rides the hand-back's
 *  router state and is shown here, once. A status, never an alert: nothing was lost. */
export function MapFullNotice({ onDismiss }: { onDismiss: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, MAP_FULL_NOTICE_MS);
    return () => clearTimeout(t);
  }, [onDismiss]);
  return (
    <div className={styles.notice} role="status" data-testid="map-full-notice">
      <span>
        <b>The map was full</b> — opened full screen. Close a window to start the next one there.
      </span>
      <button type="button" className={styles.close} onClick={onDismiss} aria-label="Dismiss">
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
