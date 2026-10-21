import { useState } from "react";
import type { StructuredTurn } from "../../types/api";
import styles from "./NotSent.module.css";
import {
  type Attachment,
  canSendAgain,
  missingPictures,
  missingPrompt,
  type NotSentChoice,
  notSentText,
} from "./notSent";

/** The not-sent row under a queued message that was proven never sent (#1538): why, in the down
 *  status colour, then Send again and Discard. Send again checks that its pictures can still be
 *  read; a missing one is asked about, never dropped quietly. While its retry is on its way the
 *  row says so instead of offering the button; once the server holds the retry (or the operator
 *  discarded the offer on this device), the row is history: muted, without actions. */
export function NotSentRow({
  turn,
  choice,
  readOnly,
  probe,
  onSendAgain,
  onDiscard,
}: {
  turn: StructuredTurn;
  choice: NotSentChoice | null;
  readOnly: boolean;
  /** Reads one picture back; rejects with a `status` when it cannot. */
  probe: (stored: string) => Promise<unknown>;
  /** Resubmit the same words with these attachments, under a new operation id. */
  onSendAgain: (attachments: Attachment[]) => void;
  onDiscard: () => void;
}) {
  const [checking, setChecking] = useState(false);
  const [missing, setMissing] = useState<Attachment[] | null>(null);
  const code = turn.not_sent?.code ?? "unexpected";
  const attachments = turn.attachments ?? [];
  const settled = choice === "again" || choice === "discarded";
  const offer = choice === null && !readOnly && canSendAgain(turn);
  const kept = missing ? attachments.filter((a) => !missing.some((m) => m.stored === a.stored)) : attachments;
  const nothingLeft = !turn.text && kept.length === 0;

  const sendAgain = async () => {
    if (checking) return;
    setChecking(true);
    try {
      const gone = await missingPictures(attachments, probe);
      if (gone.length > 0) setMissing(gone);
      else onSendAgain([...attachments]);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className={styles.notSent} data-testid="structured-not-sent" data-code={code}
      data-settled={choice ?? undefined}>
      <p className={settled ? `${styles.line} ${styles.history}` : styles.line} role={settled ? undefined : "alert"}>
        <span className={settled ? `${styles.led} ${styles.ledOff}` : `${styles.led} ${styles.ledDown}`} aria-hidden="true" />
        <span className={styles.label}>Not sent</span>{" "}
        <span className={styles.reason} data-testid="structured-not-sent-reason">{notSentText(code)}</span>
        {choice === "again" && <span className={styles.after}> Sent again below.</span>}
      </p>
      {choice === "sending" && (
        <p className={styles.hint} role="status" data-testid="structured-not-sent-sending">Sending again…</p>
      )}
      {choice === null && !readOnly && !offer && turn.text_truncated && (
        <p className={styles.hint}>This message is too long to show here in full, so it can’t be sent again from this view.</p>
      )}
      {offer && missing && (
        <div className={styles.prompt} role="group" aria-label="Missing picture" data-testid="structured-not-sent-missing">
          <p className={styles.promptText}>{missingPrompt(missing.length, nothingLeft)}</p>
          <div className={styles.actions}>
            {!nothingLeft && (
              <button type="button" className={styles.primary} onClick={() => onSendAgain(kept)}>
                Send without {missing.length === 1 ? "it" : "them"}
              </button>
            )}
            <button type="button" className={styles.action} onClick={() => setMissing(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {offer && !missing && (
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={checking} onClick={() => void sendAgain()}
            title="Sends the same message again as a new message; the first one never reached the agent">
            {checking ? "Checking…" : "Send again"}
          </button>
          <button type="button" className={styles.action} onClick={onDiscard}>
            Discard
          </button>
        </div>
      )}
    </div>
  );
}
