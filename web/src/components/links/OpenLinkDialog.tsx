import { Maximize2, SquareDashedBottom, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { engineName } from "../../app/engineRoster";
import { useSessionRow } from "../../app/useSessionRow";
import type { LinkEntry } from "../../lib/linkEntry";
import type { LinkOpenMode } from "../../lib/linkOpenMode";
import { settingsPath } from "../../routes/settingsTabs";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import styles from "./OpenLinkDialog.module.css";

/** "Open session — Full screen / In map" (#1232), for a session link that arrived from outside
 *  the app on a device that can host map windows. Choosing is the whole interaction; Esc, ✕ and
 *  the backdrop keep what is already on screen (the session, full screen) and remember nothing.
 *
 *  Drawn on the shared HUD dialog sheet and portalled to <body> like every dialog on it. */
export function OpenLinkDialog({
  entry,
  onChoose,
  onCancel,
}: {
  entry: Extract<LinkEntry, { kind: "session" }>;
  onChoose: (mode: Exclude<LinkOpenMode, "ask">, remember: boolean, title?: string) => void;
  onCancel: () => void;
}) {
  // Checked by default: remembering is what the operator asked this prompt to do, and Settings
  // resets it.
  const [remember, setRemember] = useState(true);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const firstRef = useRef<HTMLButtonElement | null>(null);
  const row = useSessionRow(`${entry.engine}:${entry.id}`);
  const title = row?.title || row?.short_uuid;
  useEffect(() => firstRef.current?.focus(), []);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const titleId = "open-link-title";
  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onCancel}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.narrow}`}
        data-testid="open-link-dialog"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Open session
          </span>
          <button
            type="button"
            className={dlg.close}
            onClick={onCancel}
            aria-label="Close open-session dialog"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>{engineName(entry.engine)} //</span>
          <b className={dlg.fromTitle}>{title || entry.id}</b>
        </p>
        <div className={styles.choices}>
          <button
            ref={firstRef}
            type="button"
            className={styles.choice}
            data-testid="open-link-fullscreen"
            onClick={() => onChoose("fullscreen", remember, title)}
          >
            <Maximize2 size={16} aria-hidden="true" />
            <span className={styles.choiceName}>Full screen</span>
            <span className={styles.choiceHelp}>The session fills the Sessions pane.</span>
          </button>
          <button
            type="button"
            className={styles.choice}
            data-testid="open-link-map"
            onClick={() => onChoose("map", remember, title)}
          >
            <SquareDashedBottom size={16} aria-hidden="true" />
            <span className={styles.choiceName}>In map</span>
            <span className={styles.choiceHelp}>
              A floating window on the map, beside your other sessions.
            </span>
          </button>
        </div>
        <label className={styles.remember}>
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            data-testid="open-link-remember"
          />
          Remember my choice on this device
        </label>
        <p className={dlg.help}>
          Change or reset it in{" "}
          <Link to={settingsPath("appearance", "opening-links")} onClick={onCancel}>
            Settings → Appearance → Opening links
          </Link>
          .
        </p>
      </div>
    </div>,
    document.body,
  );
}
