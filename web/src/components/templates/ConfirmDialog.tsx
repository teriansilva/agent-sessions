import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useFocusContainment } from "../pulse/useModalDrawer";
import { useInertBehind } from "./useInertBehind";
import { useHoldFocusWhileBusy } from "./useHoldFocusWhileBusy";
import styles from "./ConfirmDialog.module.css";

/** THE confirm dialog (#905, promoted to the app's one confirm by #1531): delete-a-template,
 *  leave-with-unsaved-edits, the changed-elsewhere choice, project deletion, the git confirms and the
 *  mission Unarchive choice. Portal to <body>, `role="dialog"`, Esc + backdrop cancel, bottom sheet
 *  ≤800px, on the HudDialog chrome.
 *
 *  Focus lands on the CANCEL action on open — the safe default for every use here — and
 *  returns to `returnFocusTo` on close. `danger` fills the confirm in the down colour, which
 *  is reserved for the one action that destroys something. */
export function ConfirmDialog({
  tag,
  title,
  children,
  cancelLabel = "Cancel",
  confirmLabel,
  confirmDisabled = false,
  danger = false,
  busy = false,
  onCancel,
  onConfirm,
  returnFocusTo,
  role = "dialog",
  markers,
  fallbackFocusSelector,
}: {
  /** The mono callsign above the title, e.g. `DELETE TEMPLATE`. */
  tag: string;
  title: string;
  children?: ReactNode;
  cancelLabel?: string;
  confirmLabel: string;
  /** Keeps the confirm action off until the dialog's own condition holds (e.g. a typed name). */
  confirmDisabled?: boolean;
  danger?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  returnFocusTo?: HTMLElement | null;
  /** `alertdialog` for a confirmation that interrupts work in progress (the git confirms). */
  role?: "dialog" | "alertdialog";
  /** Data attributes for hosts and tests: on the dialog, and on the confirm button. */
  markers?: { dialog?: Record<string, string>; confirm?: Record<string, string> };
  /** Where focus goes on close when `returnFocusTo` is gone (a git row leaves with its write). */
  fallbackFocusSelector?: string;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  // While a confirm is in flight every button is disabled: the dialog itself holds focus. It is
  // also what the containment hook focuses when nothing inside can take focus.
  useHoldFocusWhileBusy(dialogRef, busy);
  useInertBehind(returnFocusTo, fallbackFocusSelector);
  // Tab stays inside even where the surface beneath is not under the inert root (a sheet portalled
  // to <body>, like the file panel's), and the surface stack tells a drawer beneath to stand down.
  useFocusContainment({ active: true, panelRef: dialogRef });

  // `busy` means a confirm is in flight: NO dismissal path may fire (Escape, backdrop, the
  // close glyph, the footer) — a Reload-theirs chosen while an overwrite is already on the
  // wire would let that overwrite land and navigate away anyway (Hermes on #907, round 2).
  //
  // Escape is OWNED here (capture phase, propagation stopped): the topmost surface answers it, so a
  // panel or drawer beneath never closes on the same key (the git confirm's rule, #806).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        if (!busy) onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onCancel, busy]);

  return createPortal(
    <div className={styles.backdrop} onMouseDown={() => (busy ? undefined : onCancel())}>
      <div
        ref={dialogRef}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={busy || undefined}
        tabIndex={-1}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
        {...markers?.dialog}
      >
        <div className={styles.head}>
          <span className={styles.tag}>{tag}</span>
          <button
            type="button"
            className={styles.close}
            aria-label="Close"
            onClick={onCancel}
            disabled={busy}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        {children && <div className={styles.body}>{children}</div>}
        <div className={styles.actions}>
          <button
            ref={cancelRef}
            type="button"
            className={styles.cancel}
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={danger ? styles.danger : styles.confirm}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
            {...markers?.confirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
