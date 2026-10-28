import { Paperclip } from "lucide-react";
import chat from "../chat/ChatPane.module.css";
import compose from "../terminal/Compose.module.css";
import styles from "./Outbox.module.css";
import { ARRIVE_CLASS } from "./arrivals";
import { leavingNote, type OutboxEntry } from "./outbox";
import { ordinal } from "./structuredView";

/** Why BattleLab could not write the session's record (#1733), from the server's closed error
 *  class in its refusal; fixed words, never the server's text. */
function unwritableCause(reason: string | undefined): { label: string; sentence: string } {
  if (reason && /\(disk full\)/.test(reason)) {
    return { label: "disk full", sentence: "The disk is full." };
  }
  if (reason && /\(I\/O error\)/.test(reason)) {
    return { label: "I/O error", sentence: "The disk reported an I/O error." };
  }
  return { label: "record not writable", sentence: "The session’s record could not be written." };
}

/** The pending block (#1465): sent messages the conversation does not show yet, after its turns,
 *  in every API client (structured and HTTP chat alike). Each says where it stands and offers only
 *  what is safe for that state: a waiting message can be removed; a refused one edited or
 *  discarded; an unknown one only retried under its own id or discarded (an edited resend could
 *  deliver both).
 *
 *  A message refused because BattleLab could not write the session's record (#1733) is a KNOWN
 *  refusal — nothing was claimed — and shows the fallback panel instead: the draft stays here
 *  (never moved or dropped), Send again goes out as a NEW send once there is space, and Hand off
 *  is offered second, since it must write a new session too and may fail the same way. */
export function OutboxBlock({
  entries,
  agent,
  headHold,
  editBlocked,
  onRetry,
  onEdit,
  onDrop,
  onSendAgain,
  onHandOff,
  arrive = false,
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
  /** #1733: send a refused-unwritable message again as a NEW operation. Without it (a client
   *  whose refusals are all ordinary), such an entry reads as any other refusal. */
  onSendAgain?: (id: string) => void;
  /** #1733: open Hand off for this session; its own dialog reports a failure. */
  onHandOff?: (trigger: HTMLElement) => void;
  /** Each entry enters with the arrival motion (#1727): the operator's own message arrives here,
   *  at Send, and the turn that later replaces it does not arrive a second time. */
  arrive?: boolean;
}) {
  if (entries.length === 0) return null;
  const note = leavingNote(entries);
  return (
    <section className={styles.outbox} aria-label="Messages not yet in the conversation" data-testid="structured-outbox">
      {entries.map((e, i) => {
        const { id, text, attachments } = e.request;
        if (e.phase === "refused" && e.unwritable && onSendAgain) {
          const cause = unwritableCause(e.reason);
          return (
            <div key={id} className={arrive ? `${styles.pending} ${ARRIVE_CLASS}` : styles.pending}
              data-testid="structured-pending" data-phase={e.phase} data-unwritable=""
              data-operation={id}>
              <div className={`${chat.turn} ${chat.user}`}>
                <div className={chat.who}>you · not sent</div>
                {!!text && <div className={`${chat.txt} ${styles.text} ${styles.failedText}`}>{text}</div>}
              </div>
              <p className={`${styles.state} ${styles.refused}`} data-testid="structured-pending-state">
                Not sent — {cause.label}, nothing was claimed
              </p>
              <section className={styles.unwritable} role="region"
                aria-label="Session record can’t be written" data-testid="structured-unwritable">
                <div className={styles.unwritableTitle}>
                  <span aria-hidden="true">■ </span>BattleLab can’t write this session right now
                </div>
                <p className={styles.unwritableText}>
                  {cause.sentence} Free space, then send again — your text stays below until it’s
                  sent. Hand off also needs to write a new session, so it may fail until there’s space.
                </p>
                <div className={styles.draft} data-testid="structured-unwritable-draft"
                  aria-label="Your message, not sent yet">
                  {attachments.length > 0 && (
                    <div className={styles.files}>
                      {attachments.map((a) => (
                        <span key={a.stored} className={`${compose.pill} ${styles.file}`} title={a.name}>
                          <Paperclip size={14} aria-hidden />
                          <span className={compose.pn}>{a.name}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  {text}
                </div>
                <div className={styles.unwritableActions}>
                  <button type="button" className={styles.primary} onClick={() => onSendAgain(id)}
                    title="Sends these words again as a new message; nothing was sent before">
                    Send again
                  </button>
                  {onHandOff && (
                    <button type="button" className={styles.action}
                      onClick={(ev) => onHandOff(ev.currentTarget)}
                      title="Start a new session seeded with this one — it may also fail while the disk is full">
                      Hand off to a new session
                    </button>
                  )}
                  <button type="button" className={styles.action} onClick={() => onDrop(id)}>
                    Discard
                  </button>
                </div>
              </section>
            </div>
          );
        }
        const label =
          e.phase === "sending"
            ? "sending"
            : e.phase === "waiting"
              ? `waiting to send · ${ordinal(i + 1)}`
              : e.phase === "refused"
                ? "not sent"
                : "unknown";
        return (
          <div key={id} className={arrive ? `${styles.pending} ${ARRIVE_CLASS}` : styles.pending}
            data-testid="structured-pending" data-phase={e.phase}
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
