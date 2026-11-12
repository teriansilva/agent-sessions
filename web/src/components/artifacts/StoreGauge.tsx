/** The artifact store's usage as a segmented gauge (#1932 P2). Not the shared `Gauge`: that one reads
 *  a LOW fill as a warning (a remaining budget), and here a low fill is the healthy case. At least
 *  one segment is lit once anything is stored, so a few MB never read as empty. */
import styles from "./artifacts.module.css";

export function StoreGauge({
  bytes,
  total,
  segments = 20,
}: {
  bytes: number;
  total: number;
  segments?: number;
}) {
  const v = total > 0 ? Math.max(0, Math.min(1, bytes / total)) : 0;
  const on = bytes > 0 ? Math.max(1, Math.round(v * segments)) : 0;
  return (
    <div
      className={`nj-gauge ${styles.gauge}`}
      role="meter"
      aria-label="Artifact storage used"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(v * 100)}
    >
      <div className="nj-track" aria-hidden="true">
        {Array.from({ length: segments }, (_, i) => (
          <i key={i} className={i < on ? "on" : undefined} />
        ))}
      </div>
    </div>
  );
}
