import { Lock } from "lucide-react";
import { useEffect, useRef } from "react";
import type { TermGateHolder } from "../../lib/termSocket";
import styles from "./GateOverlay.module.css";

/** The single-active-viewer take-over gate (#293). Shown over a blurred terminal when this
 *  device is NOT the active viewer — either it opened a session already active elsewhere
 *  ("busy") or it was just taken over mid-session ("taken"). One page, two messages.
 *  Take over reconnects with force=1 (promotes this device); Cancel goes to the new-session
 *  page. The holder `label` is server-echoed and untrusted — React escapes it on render. */
export function GateOverlay({
  holder,
  mode,
  onTakeover,
  onCancel,
}: {
  holder: TermGateHolder | null;
  mode: "busy" | "taken";
  onTakeover: () => void;
  onCancel: () => void;
}) {
  const takeoverRef = useRef<HTMLButtonElement>(null);

  // Take over is the primary action: focus it on mount, and Esc = Cancel (→ new session).
  useEffect(() => {
    takeoverRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const name = holder?.label?.trim() || "another device";
  const heading = mode === "taken" ? "Control taken" : "Session in use";
  const detail =
    mode === "taken" ? (
      <>
        <b className={styles.who}>{name}</b> took control
        {sinceSuffix(holder?.since, true)}
      </>
    ) : (
      <>
        Active on <b className={styles.who}>{name}</b>
        {sinceSuffix(holder?.since, false)}
      </>
    );

  return (
    <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="Session take-over">
      <div className={styles.card}>
        <Lock className={styles.icon} size={26} aria-hidden="true" />
        <div className={styles.heading}>{heading}</div>
        <div className={styles.detail}>{detail}</div>
        <div className={styles.actions}>
          <button
            ref={takeoverRef}
            type="button"
            className={styles.takeover}
            onClick={onTakeover}
          >
            Take over
          </button>
          <button type="button" className={styles.cancel} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/** " · just now" / " · since 12:04". Best-effort; hidden if there's no usable timestamp. */
function sinceSuffix(since: number | undefined, taken: boolean) {
  if (!since || !Number.isFinite(since)) return null;
  const ms = since * 1000;
  const ageS = (Date.now() - ms) / 1000;
  if (taken && ageS >= 0 && ageS < 45) return <span className={styles.since}> · just now</span>;
  try {
    const when = new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return <span className={styles.since}> · since {when}</span>;
  } catch {
    return null;
  }
}
