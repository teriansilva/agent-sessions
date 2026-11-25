import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useMapWindows } from "../../app/workspaceWindows";
import { openFullScreen, type LaunchOutcome } from "../../lib/newSessionLaunch";
import { NewSessionForm } from "../../routes/NewSessionLanding";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import styles from "./NewSessionModal.module.css";

/** New session over the map (#1648). The map and its windows stay mounted the whole time, so no
 *  terminal socket closes and no API pane stops polling. The form is the page's own
 *  (`NewSessionForm`), never a copy. It emits what it launched; THIS host decides where it opens:
 *  a window in place when the map can host one and has room (read at the moment of launch, so a
 *  window opened or a cap lowered meanwhile is honoured), otherwise full screen. A refusal at the
 *  canvas's drain still hands the session back to its full-screen route (`handBack`).
 *
 *  THE dialog's chrome (HudDialog), portalled to `<body>` so it stacks above both `<ReactFlow>`
 *  and the windows overlay (`map-window-layer.md`). On a phone it is a FULL-HEIGHT sheet, not
 *  the shared 88vh bottom sheet: the form is a whole page's worth of fields.
 *
 *  - Esc, the ✕ and the backdrop close it, except while a create is in flight: closing then
 *    would drop the window the outcome opens.
 *  - A nested dialog (a picker opened from the form) takes Esc first: it is the topmost
 *    `aria-modal`, so this one stands down.
 *  - Focus moves in on open, Tab stays in, and focus returns to the trigger on close. */
export function NewSessionModal({
  onClose,
  returnFocusTo,
}: {
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [busy, setBusy] = useState(false);
  // Says what Start does HERE: a map that can host a window opens it in place; one that cannot
  // (a phone, a short window) opens the session full screen.
  const ws = useMapWindows();
  const canHost = !!ws?.mapReady;
  const navigate = useNavigate();
  const onLaunched = (outcome: LaunchOutcome) => {
    if (ws?.mapReady && ws.hasRoom) {
      if (outcome.fresh) ws.requestOpen(outcome.seed, outcome.fresh);
      else ws.requestOpen(outcome.seed);
      onClose();
      return;
    }
    openFullScreen(navigate, outcome); // the map route unmounts, and this modal with it
  };
  const close = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);

  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      // The topmost modal owns Esc: a nested dialog opened from the form closes first.
      const modals = document.querySelectorAll('[aria-modal="true"]');
      if (modals[modals.length - 1] !== dialogRef.current) return;
      e.preventDefault();
      close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [close]);

  const titleId = "new-session-modal-title";
  return createPortal(
    <div className={`${dlg.backdrop} ${styles.backdrop}`} onMouseDown={close}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={busy || undefined}
        className={`${dlg.dialog} ${styles.dialog}`}
        data-testid="new-session-modal"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={`${dlg.head} ${styles.head}`}>
          <h2 id={titleId} className={dlg.tag}>
            New session // on the map
          </h2>
          <button
            ref={closeRef}
            type="button"
            className={dlg.close}
            onClick={close}
            disabled={busy}
            aria-label="Close new session"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <p className={styles.note} data-testid="new-session-modal-note">
          {canHost
            ? "The map stays live behind this. Start opens the session as a window here."
            : "The map stays behind this sheet until you start. Start opens the session full screen."}
        </p>
        <div className={styles.body}>
          <NewSessionForm host="map" onLaunched={onLaunched} onBusy={setBusy} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
