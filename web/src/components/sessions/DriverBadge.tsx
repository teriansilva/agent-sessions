import type { SessionDriver } from "../../types/api";
import styles from "./DriverBadge.module.css";
import { driverLabel } from "./driverLabel";

function driverTitle(d: SessionDriver): string {
  if (d.who === "hq") {
    return `HQ drives this session${d.task_label ? ` (${d.task_label})` : ""}. Typing in it takes it over.`;
  }
  if (d.who === "mission") return "A mission drives this session.";
  return "You drive this session.";
}

/** Who drives a session (#2398), one mono pill: on every sidebar row and in a map window's
 *  chrome bar (the window's ONLY bar, `map-window-layer.md`), fed by the row's server fact. It
 *  never shrinks: the row's meta line ellipsizes its trailing text instead. */
export function DriverBadge({ driver }: { driver: SessionDriver | undefined }) {
  if (!driver) return null;
  return (
    <span
      className={`${styles.badge} ${driver.who === "hq" ? styles.hq : driver.who === "mission" ? styles.mission : styles.you}`}
      title={driverTitle(driver)}
      data-testid="driver-badge"
      data-driver={driver.who}
    >
      {driverLabel(driver)}
    </span>
  );
}
