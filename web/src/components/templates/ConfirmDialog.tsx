import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useInertBehind } from "./useInertBehind";
import styles from "./ConfirmDialog.module.css";

/** A small confirm dialog for the templates surfaces (#905): delete-a-template, leave-with-
 *  unsaved-edits, and the changed-elsewhere choice. Same vocabulary as `SentMessagesModal`
 *  (portal to <body>, `role="dialog"`, Esc + backdrop cancel, bottom sheet ≤800px), kept local
 *  to this feature rather than promoted to a global primitive.
 *
 *  Focus lands on the CANCEL action on open — the safe default for every use here — and
 *  returns to `returnFocusTo` on close. `danger` paints the confirm in the down colour, which
 *  is reserved for the one action that destroys authored text. */
export function ConfirmDialog({
  tag,
  title,
  children,
  cancelLabel = "Cancel",
  confirmLabel,
  danger = false,
  busy = false,
  onCancel,
  onConfirm,
  returnFocusTo,
}: {
  /** The mono callsign above the title, e.g. `DELETE TEMPLATE`. */
  tag: string;
  title: string;
  children?: ReactNode;
  cancelLabel?: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  useInertBehind(returnFocusTo);

  // `busy` means a confirm is in flight: NO dismissal path may fire (Escape, backdrop, the
  // close glyph, the footer) — a Reload-theirs chosen while an overwrite is already on the
  // wire would let that overwrite land and navigate away anyway (Hermes on #907, round 2).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        if (!busy) onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel, busy]);

  return createPortal(
    <div className={styles.backdrop} onMouseDown={() => (busy ? undefined : onCancel())}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
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
            className={`${styles.confirm} ${danger ? styles.danger : ""}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
