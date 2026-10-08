import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import buttons from "../ui/actionButton.module.css";
import hud from "../HudDialog.module.css";
import styles from "./playbooks.module.css";

/** Native modal focus containment, shared HUD chrome, and a scrollable body at phone heights. */
export function PlaybookDialog({
  tag,
  title,
  children,
  confirmLabel,
  busy = false,
  danger = false,
  onCancel,
  onConfirm,
  returnFocusTo,
}: {
  tag: string;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    cancel.current?.focus();
    return () => {
      node.close();
      if (returnFocusTo?.isConnected) returnFocusTo.focus();
    };
  }, [returnFocusTo]);
  return createPortal(
    <dialog
      ref={dialog}
      className={`${hud.dialog} ${styles.dialog}`}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <div className={hud.head}>
        <span className={hud.tag}>{tag}</span>
        <button
          className={buttons.icon}
          aria-label="Close"
          disabled={busy}
          onClick={onCancel}
        >
          ×
        </button>
      </div>
      <h2 id={titleId}>{title}</h2>
      <div className={styles.dialogBody}>{children}</div>
      <div className={hud.actions}>
        <button
          ref={cancel}
          className={buttons.ghost}
          disabled={busy}
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          className={`${buttons.primary} ${danger ? styles.danger : ""}`}
          disabled={busy}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>,
    document.body,
  );
}
