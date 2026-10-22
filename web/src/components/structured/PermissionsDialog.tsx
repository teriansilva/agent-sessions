import { useEffect, useId, useRef, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useModalDrawer } from "../pulse/useModalDrawer";
import { useHoldFocusWhileBusy } from "../templates/useHoldFocusWhileBusy";
import dialog from "../HudDialog.module.css";
import styles from "./StructuredPane.module.css";

export function PermissionsDialog({
  busy,
  retry,
  active,
  error,
  onConfirm,
  onClose,
  returnFocusTo,
  fallbackFocusRef,
}: {
  busy: boolean;
  retry: boolean;
  active: boolean;
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
  returnFocusTo: HTMLElement | null;
  fallbackFocusRef: RefObject<HTMLElement | null>;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef(returnFocusTo);
  const title = useId();
  useModalDrawer({
    active: true,
    panelRef,
    initialFocusRef: cancelRef,
    triggerRef,
    onClose: () => {
      if (!busy) onClose();
    },
  });
  useEffect(() => () => {
    // Successful enabling removes its own header action. Keep keyboard users in this pane
    // when that trigger no longer exists; the shared modal hook restores surviving triggers.
    requestAnimationFrame(() => {
      if (!triggerRef.current?.isConnected) fallbackFocusRef.current?.focus();
    });
  }, [fallbackFocusRef]);
  // While enabling runs both buttons are disabled: the dialog itself holds focus (#1560).
  useHoldFocusWhileBusy(panelRef, busy);
  return createPortal(
    <div className={dialog.backdrop}>
      <div
        ref={panelRef}
        tabIndex={-1}
        aria-busy={busy || undefined}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title}
        className={`${dialog.dialog} ${dialog.narrow}`}
      >
        {/* THE dialog's callsign head (#1531). No ✕: Cancel and Escape close it, and every button
            here holds the 44px floor (e2e/live-permissions.spec.ts). */}
        <div className={dialog.head}>
          <h2 id={title} className={dialog.tag}>
            Enable Skip permissions?
          </h2>
        </div>
        <p className={dialog.muted}>This session will run tools without asking for permission.</p>
        <p className={styles.permissionNotice}>
          The current turn, queued messages, and pending approvals will stop. Your conversation
          stays here. Send your next message to continue with Skip permissions enabled.
        </p>
        <p className={dialog.muted}>
          This applies only to this session, including when you reopen it.
        </p>
        {busy && (
          <p role="status" className={dialog.help}>
            Stopping the session’s tools before enabling…
          </p>
        )}
        {error && (
          <p role="alert" className={dialog.error}>
            {error}
          </p>
        )}
        <div className={dialog.actions}>
          <button
            ref={cancelRef}
            type="button"
            className={dialog.cancel}
            disabled={busy}
            onClick={onClose}
          >
            {retry ? "Close" : "Cancel"}
          </button>
          <button type="button" className={dialog.go} disabled={busy} onClick={onConfirm}>
            {busy
              ? "Enabling…"
              : retry
                ? "Retry enabling"
                : active
                  ? "Stop turn & enable"
                  : "Enable Skip permissions"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
