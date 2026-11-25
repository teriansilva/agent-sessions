/** "⟳ AUTO · <name>" (#1201 §5): this session or mission was started by an automation, not by you.
 *
 *  Membership, not a status — so the accent vocabulary of the mission tag beside it, never a status
 *  colour. The name is the automation's CURRENT name (the server joins it), or the one it had when
 *  it was deleted; the title says which. Fed by `useAutomationOrigins` — one small map, no poll of
 *  its own. */
import type { AutomationOrigin } from "../../types/automations";
import styles from "./automations.module.css";

export function OriginBadge({
  origin,
  className,
}: {
  origin: AutomationOrigin;
  className?: string;
}) {
  const title = origin.deleted
    ? `Started by the automation “${origin.name}” (since deleted)`
    : `Started by the automation “${origin.name}”`;
  return (
    <span
      className={`${styles.originBadge} ${className ?? ""}`}
      title={title}
      data-testid="origin-badge"
    >
      <span aria-hidden="true">⟳</span>
      <span className={styles.originText}>Auto · {origin.name}</span>
    </span>
  );
}
