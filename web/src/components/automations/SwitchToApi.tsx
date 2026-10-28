/** API first for automations (#1728 Phase 2): the legacy label on a terminal target, and
 *  "Switch to API client" — a FRESH consent through the existing validated save.
 *
 *  - The label is a neutral stamp, not a status colour: it classifies what the action targets, not
 *    a live state (the automation's own state keeps its LED and word). It is shown for ANY console
 *    target — nothing is stamped on the row — and the automation keeps running.
 *  - Switch opens a sheet naming the new client, its model and its permission mode, and saves only
 *    on Confirm, through `PATCH` (the editor's own save). The terminal's permission grant is never
 *    carried over: the switch asks before each request. An enabled automation then asks for the
 *    server's consent to the new scope (`ConsentDialog`, the server's lines and digest). The
 *    server validates the client's model and permission settings and refuses what it cannot run.
 *  - No API client admitted for unattended use: the button stays, disabled, with the server's
 *    reason. Never a terminal fallback. */
import { engineLabel } from "../../app/engineRoster";
import styles from "./automations.module.css";

/** "Terminal — unreliable unattended": the neutral stamp a console-target automation carries. */
export function LegacyTerminalStamp() {
  return (
    <span
      className={styles.legacyStamp}
      data-testid="automation-legacy-terminal"
      title="It runs in a terminal, where nobody is watching to answer what it asks. It keeps running; an API client runs this kind of work reliably."
    >
      Terminal — unreliable unattended
    </span>
  );
}

/** The 44 px "Switch to API client" button, or — when the API client cannot be started unattended
 *  yet — the same button disabled, with the server's reason beside it. */
export function SwitchToApiButton({
  target,
  busy = false,
  blockedBy,
  onSwitch,
}: {
  target: { engine: string; ok: boolean; reason: string };
  busy?: boolean;
  /** Why it cannot switch right now for a reason of the page's own (an unsaved draft). */
  blockedBy?: string;
  onSwitch: () => void;
}) {
  const why = !target.ok ? `${engineLabel(target.engine)}: ${target.reason}` : blockedBy;
  return (
    <span className={styles.switchWrap}>
      <button
        type="button"
        className={styles.ghost}
        disabled={busy || !!why}
        onClick={onSwitch}
        data-testid="automation-switch-api"
      >
        Switch to API client
      </button>
      {why && (
        <small className={styles.faint} data-testid="automation-switch-why">
          {why}
        </small>
      )}
    </span>
  );
}
