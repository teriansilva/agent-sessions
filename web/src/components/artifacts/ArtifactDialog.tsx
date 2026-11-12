/** The artifacts dialogs' shell (#1932 P2): the activation dialog and "public links are off" use it.
 *  It is the HudDialog chrome (portalled, modal, the page behind inert, Tab contained, Esc and an
 *  outside press close, focus back on the opener, a bottom sheet at ≤800px); the share confirm is
 *  the shared ConfirmDialog instead, because it confirms something. It stacks where ConfirmDialog
 *  does, above the Files panel's phone sheet that the session tab opens it from. */
import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import { useInertBehind } from "../templates/useInertBehind";
import styles from "./artifacts.module.css";

export function ArtifactDialog({
  tag,
  value,
  title,
  children,
  actions,
  onClose,
  returnFocusTo,
  testId,
}: {
  tag: string;
  /** The callsign's value after `//` (e.g. "this session"). */
  value?: string;
  /** A heading under the callsign, when the dialog asks or states one thing. */
  title?: string;
  children: ReactNode;
  /** The footer's buttons; a Close is always offered by the head's ✕ as well. */
  actions: ReactNode;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  testId?: string;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const titleId = useId();
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useInertBehind(returnFocusTo, "#root");
  useFocusContainment({ active: true, panelRef: panel });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return createPortal(
    <div className={`${dlg.backdrop} ${styles.stacked}`} onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={dlg.dialog}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid={testId}
      >
        <div className={dlg.head}>
          <h2 id={titleId} className={dlg.tag}>
            {tag}
            {value && (
              <>
                <span aria-hidden="true">//</span>
                <span>{value}</span>
              </>
            )}
          </h2>
          <button
            ref={closeRef}
            type="button"
            className={dlg.close}
            aria-label="Close"
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        {title && <p className={styles.dTitle}>{title}</p>}
        {children}
        <div className={dlg.actions}>{actions}</div>
      </div>
    </div>,
    document.body,
  );
}
