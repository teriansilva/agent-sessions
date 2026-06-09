import { useAppVersion } from "./useAppVersion";
import styles from "./NewVersionBanner.module.css";

/** Tiny banner that appears when the server's `/api/version` no longer matches what this
 *  tab booted with — i.e. a deploy happened while this tab was open and the SPA bundle is
 *  now stale. Click → location.reload() to pick up the fresh `index.html` + chunks (#169).
 *
 *  Intentionally NOT auto-reload: a silent reload mid-prompt would be worse than the stale
 *  bundle. The user picks their moment. */
export function NewVersionBanner() {
  const { hasNewVersion } = useAppVersion();
  if (!hasNewVersion) return null;
  return (
    <div className={styles.banner} role="status" aria-live="polite">
      <span>A new version is available.</span>
      <button type="button" onClick={() => window.location.reload()}>
        Reload
      </button>
    </div>
  );
}
