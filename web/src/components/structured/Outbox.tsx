import { Paperclip } from "lucide-react";
import chat from "../chat/ChatPane.module.css";
import compose from "../terminal/Compose.module.css";
import styles from "./Outbox.module.css";
import { leavingNote, type OutboxEntry } from "./outbox";
import { ordinal } from "./structuredView";

/** The pending block (#1465): sent messages the conversation does not show yet, after its turns,
 *  in every API client (structured and HTTP chat alike). Each says where it stands and offers only
 *  what is safe for that state: a waiting message can be removed; a refused one edited or
 *  discarded; an unknown one only retried under its own id or discarded (an edited resend could
 *  deliver both). */
export function OutboxBlock({
  entries,
  agent,
  headHold,
  editBlocked,
  onRetry,
  onEdit,
  onDrop,
}: {
  entries: readonly OutboxEntry[];
  agent: string;
  /** Why the waiting head is not going out yet, or null when it goes next. */
  headHold: string | null;
  /** The composer holds a message: Edit would replace it. */
  editBlocked: boolean;
  onRetry: (id: string) => void;
  onEdit: (id: string) => void;
  onDrop: (id: string) => void;
}) {
  if (entries.length === 0) return null;
  const note = leavingNote(entries);
  return (
    <section className={styles.outbox} aria-label="Messages not yet in the conversation" data-testid="structured-outbox">
      {entries.map((e, i) => {
        const { id, text, attachments } = e.request;
        const label =
          e.phase === "sending"
            ? "sending"
            : e.phase === "waiting"
              ? `waiting to send · ${ordinal(i + 1)}`
              : e.phase === "refused"
                ? "not sent"
                : "unknown";
        return (
          <div key={id} className={styles.pending} data-testid="structured-pending" data-phase={e.phase}
            data-operation={id}>
            <div className={`${chat.turn} ${chat.user}`}>
              <div className={chat.who}>you · {label}</div>
              {attachments.length > 0 && (
                <div className={styles.files}>
                  {attachments.map((a) => (
                    <span key={a.stored} className={`${compose.pill} ${styles.file}`} title={a.name}
                      data-testid="structured-pending-attachment">
                      <Paperclip size={14} aria-hidden />
                      <span className={compose.pn}>{a.name}</span>
                    </span>
                  ))}
                </div>
              )}
              {!!text && <div className={`${chat.txt} ${styles.text}`}>{text}</div>}
            </div>
            <p className={
              e.phase === "refused"
                ? `${styles.state} ${styles.refused}`
                : e.phase === "unknown"
                  ? `${styles.state} ${styles.unknown}`
                  : styles.state
            } data-testid="structured-pending-state">
              {e.phase === "sending"
                ? e.recorded ? "Recorded by BattleLab · updating the conversation…" : "Sending to BattleLab…"
                : e.phase === "waiting"
                  ? i === 0 ? headHold ?? "Goes out next." : "Goes out after the message above."
                  : e.phase === "refused"
                    ? `Not sent: ${e.reason ?? "refused"}`
                    : "May not have reached BattleLab. Retrying sends the same message; it won’t be duplicated."}
            </p>
            {e.phase !== "sending" && (
              <div className={styles.actions}>
                {e.phase === "unknown" && (
                  <button type="button" className={styles.primary} onClick={() => onRetry(id)}
                    title="Sends the same message again; BattleLab keeps it once">
                    Retry
                  </button>
                )}
                {e.phase === "refused" && (
                  <button type="button" className={styles.action} disabled={editBlocked}
                    title={editBlocked ? "Send or clear the message you are writing first" : `Put it back in the message to ${agent}`}
                    onClick={() => onEdit(id)}>
                    Edit
                  </button>
                )}
                <button type="button" className={styles.action} onClick={() => onDrop(id)}
                  aria-label={e.phase === "waiting" ? "Remove this waiting message" : undefined}>
                  {e.phase === "waiting" ? "Remove" : e.phase === "unknown" ? "Discard — it may still arrive" : "Discard"}
                </button>
              </div>
            )}
          </div>
        );
      })}
      {note && (
        <p className={styles.note} data-testid="structured-outbox-note">
          {note}
        </p>
      )}
    </section>
  );
}
