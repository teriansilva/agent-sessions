import { Copy, RotateCcw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { relTime } from "../../lib/format";
import type { SentMessage } from "../../lib/sentHistory";
import styles from "./SentMessagesModal.module.css";

/** Sent-messages recovery modal (#619): the history chip in the compose key bar opens this.
 *  Lists the last 10 compose submissions on this device, newest first — each recorded BEFORE
 *  delivery, so a send the agent silently swallowed (#616) is still here. Restore drops one back
 *  into the composer; Copy puts it on the clipboard.
 *
 *  `UNCONFIRMED` is the only delivery claim made, and it is a negative one: it means the client
 *  knows a frame did not reach the socket. Its absence never asserts the agent processed anything.
 *
 *  Accessibility mirrors SessionRecapModal: role="dialog"/aria-modal, labelled by the heading,
 *  focus moves in on open and returns to the trigger on close, Esc + backdrop click close. */
export function SentMessagesModal({
  entries,
  currentSession,
  onRestore,
  onClose,
  returnFocusTo,
}: {
  /** Newest first. */
  entries: SentMessage[];
  /** Engine-qualified id of the session this composer belongs to (null for a fresh launch). */
  currentSession: string | null;
  onRestore: (entry: SentMessage) => void;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  // Per-row transient feedback for Copy — the clipboard can fail (insecure origin) and must say so.
  const [copied, setCopied] = useState<{ id: string; ok: boolean } | null>(
    null,
  );

  useEffect(() => {
    closeRef.current?.focus();
    return () => returnFocusTo?.focus?.();
  }, [returnFocusTo]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copy = (entry: SentMessage) => {
    const write = navigator.clipboard?.writeText(entry.text);
    if (!write) {
      setCopied({ id: entry.id, ok: false });
      return;
    }
    void write.then(
      () => setCopied({ id: entry.id, ok: true }),
      () => setCopied({ id: entry.id, ok: false }),
    );
  };

  const titleId = "sent-messages-title";

  // Portal to <body>: the terminal pane is a backdrop-filter containing block, which would
  // otherwise clip a position:fixed overlay to the pane instead of the full viewport.
  return createPortal(
    <div className={styles.backdrop} onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <span id={titleId} className={styles.tag}>
            Sent messages · last {entries.length}
          </span>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            onClick={onClose}
            aria-label="Close sent messages"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <ul className={styles.list}>
          {entries.map((e) => (
            <li key={e.id} className={styles.msg}>
              <div className={styles.meta}>
                <span className={styles.when}>
                  {relTime(Math.floor(e.ts / 1000))}
                </span>
                {!e.confirmed && (
                  <span
                    className={styles.badge}
                    title="This send never reached the socket"
                  >
                    Unconfirmed
                  </span>
                )}
                {e.session !== currentSession && (
                  <span className={styles.other}>· other session</span>
                )}
              </div>
              <p className={styles.body}>{e.text}</p>
              {e.attachments.length > 0 && (
                <p className={styles.attachments}>
                  {e.attachments.length} attachment
                  {e.attachments.length === 1 ? "" : "s"}
                </p>
              )}
              <div className={styles.acts}>
                <button
                  type="button"
                  className={`${styles.act} ${styles.primary}`}
                  onClick={() => onRestore(e)}
                >
                  <RotateCcw size={12} aria-hidden="true" />
                  Restore
                </button>
                <button
                  type="button"
                  className={styles.act}
                  onClick={() => copy(e)}
                >
                  <Copy size={12} aria-hidden="true" />
                  {copied?.id === e.id
                    ? copied.ok
                      ? "Copied"
                      : "Copy failed"
                    : "Copy"}
                </button>
              </div>
            </li>
          ))}
        </ul>

        <p className={styles.foot}>
          Kept on this device · newest first · cleared on sign-out
        </p>
      </div>
    </div>,
    document.body,
  );
}
