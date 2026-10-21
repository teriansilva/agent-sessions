import { useEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { X } from "lucide-react";
import { useSessionsStore } from "../../app/sessionsStore";
import { engineBadge, sessionPathFromKey, shortCwd } from "../../lib/format";
import { sessionStatus } from "../../lib/sessionStatus";
import type { Session, Template } from "../../types/api";
import { useInertBehind } from "./useInertBehind";
import styles from "./TemplatePickerModal.module.css";
import { useEngineRoster } from "../../app/engineRoster";

/** USE on a gallery card (#905 P3): pick a session, land in its pane with the template staged,
 *  and the composer's picker opens on it. The rows are the sidebar's own store — the page the
 *  sidebar has loaded, which is the sessions the operator can see right now — sorted working
 *  first, then most recent. The staging travels as router state that `SessionView` consumes
 *  once; nothing here sends. */
export function SessionChooserModal({
  template,
  onClose,
  returnFocusTo,
}: {
  template: Template;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const navigate = useNavigate();
  const { sessions } = useSessionsStore();
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = "session-chooser-title";

  const rows = useMemo(
    () =>
      sessions
        .filter((s) => !s.archived)
        .sort(
          (a, b) =>
            Number(Boolean(b.working)) - Number(Boolean(a.working)) ||
            (b.last_mtime ?? 0) - (a.last_mtime ?? 0),
        ),
    [sessions],
  );

  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useInertBehind(returnFocusTo);

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

  const pick = (s: Session) => {
    const path = sessionPathFromKey(s.id);
    if (!path) return;
    onClose();
    navigate(path, { state: { template: template.id } });
  };

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
            Use “{template.name}” // pick a session
          </span>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            aria-label="Close"
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        {rows.length === 0 ? (
          <p className={styles.state}>No sessions loaded — open one from the sidebar first.</p>
        ) : (
          <ul className={styles.list} aria-label="Sessions">
            {rows.map((s) => {
              const st = sessionStatus(s);
              return (
                <li key={s.id} className={styles.row}>
                  <button
                    type="button"
                    className={styles.srow}
                    onClick={() => pick(s)}
                    aria-label={`Use in ${s.title || s.first_user_message || s.short_uuid}`}
                  >
                    <span className={styles.badge}>{engineBadge(s.engine)}</span>
                    <span className={styles.stitle}>
                      <span className={styles.name}>
                        {s.title || s.first_user_message || s.short_uuid}
                      </span>
                      <span className={styles.sub}>{shortCwd(s.cwd)}</span>
                    </span>
                    <span
                      className={`${styles.sstate} ${st.variant === "up" ? styles.sstateUp : ""}`}
                    >
                      {st.variant === "up" ? "working" : st.variant === "attention" ? "needs you" : "idle"}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <p className={styles.note}>
          The composer opens on this template — fill its fields, then Send or Insert.
        </p>
      </div>
    </div>,
    document.body,
  );
}
