import { useEffect, useRef, useState } from "react";
import type { LinkEntry } from "../../lib/linkEntry";
import styles from "./HandoffStub.module.css";

/** What a new tab shows after an existing BattleLab tab took its link (#1232).
 *
 *  A page cannot raise another tab, and may close itself only if a script opened it — so "Close
 *  this tab" is best effort, and says so when the browser refuses. "Open here instead" boots the
 *  app in this tab after all, on the same link. */
export function HandoffStub({
  entry,
  onOpenHere,
}: {
  entry: LinkEntry;
  onOpenHere: () => void;
}) {
  const [refused, setRefused] = useState(false);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => closeRef.current?.focus(), []);
  const what = entry.kind === "session" ? "The session" : "The mission";
  return (
    <main className={styles.page} data-testid="link-handoff-stub">
      <section className={styles.card} aria-labelledby="handoff-title">
        <h1 id="handoff-title" className={styles.title}>
          Opened in your other BattleLab tab
        </h1>
        <p className={styles.text}>
          {what} is open there now. You can close this tab. Installing BattleLab as an app
          opens links in its own window instead.
        </p>
        <div className={styles.actions}>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            onClick={() => {
              window.close();
              // Still here a moment later: the browser kept a tab it did not let a script open.
              setTimeout(() => setRefused(true), 150);
            }}
          >
            Close this tab
          </button>
          <button type="button" className={styles.here} onClick={onOpenHere}>
            Open here instead
          </button>
        </div>
        {refused && (
          <p className={styles.note} role="status">
            Your browser keeps this tab open — close it from the tab bar.
          </p>
        )}
      </section>
    </main>
  );
}
