/** A queued message that was proven not sent (#1538): its reason, and Send again.
 *
 * The server says WHY with a closed code (`turn.not_sent.code`); the words come from this fixed
 * table, never from server or CLI text. Send again resubmits the same words and attachments as a
 * NEW operation through the composer's outbox (#1465): the original was settled `not_sent` before
 * anything reached the agent, so a new id cannot deliver it twice. */
import type { StructuredTurn } from "../../types/api";

export const NOT_SENT_TEXT: Readonly<Record<string, string>> = Object.freeze({
  agent_removed: "this session’s agent was removed while the message waited — nothing was delivered.",
  agent_changed:
    "this session’s agent was switched to a different install while the message waited — nothing was delivered.",
  source_store_changed:
    "this session’s history store changed owner while the message waited — nothing was delivered.",
  generation_closed: "the session was stopped before this message could go out — nothing was delivered.",
  question_replaced: "the question it answered was answered or replaced first — nothing was delivered.",
  worker_ended: "the session ended while this message waited — nothing was delivered.",
  unexpected: "BattleLab could not hand this message to the agent — nothing was delivered.",
});

/** The fixed reason for a code; an unknown code (a newer server) reads as `unexpected`. */
export function notSentText(code: string): string {
  return Object.prototype.hasOwnProperty.call(NOT_SENT_TEXT, code)
    ? NOT_SENT_TEXT[code]
    : NOT_SENT_TEXT.unexpected;
}

/** Whether the pane may offer Send again for this turn. Only a queued operator message proven not
 *  sent, sent by the operator (a public-link visitor's words are not resent as the operator's),
 *  whose words this view holds in full. Never a Send now control (not a turn), an uncertain or a
 *  delivered turn: none of those carries `not_sent`. Nor one whose retry the server already holds:
 *  if that retry failed, its own row offers the next attempt. */
export function canSendAgain(turn: StructuredTurn): boolean {
  return (
    !!turn.not_sent?.retry &&
    !turn.not_sent.retried &&
    !turn.not_sent.retry_recorded &&
    turn.state === "failed" &&
    !turn.author &&
    !turn.text_truncated
  );
}

/** Where a not-sent message stands. `again` and `sending` come from the server and the outbox:
 *  `again` — the server holds its retry operation, whatever became of it (`retry_recorded`, from
 *  any tab or device); a retry that failed before stdin is its own not-sent row below, offering
 *  Send again under ITS derived id, so a chain is recovered at its newest link, never twice;
 *  `sending` — its retry is in this tab's outbox, waiting, on its way or of unknown outcome.
 *  `discarded` only hides the offer on this device and never implies delivery. */
export type NotSentChoice = "again" | "sending" | "discarded";

export const NOT_SENT_KEY = "agent-sessions.not-sent-discarded";
const LIMIT = 500;
/** When storage refuses (private mode, quota), Discard holds for this page. */
const pageOnly = new Set<string>();

type Stored = Record<string, number>;

function read(): Stored {
  try {
    const raw = localStorage.getItem(NOT_SENT_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Stored) : {};
  } catch {
    return {};
  }
}

const keyOf = (session: string, turnId: string) => `${session} ${turnId}`;

export function notSentDiscarded(session: string, turnId: string): boolean {
  const key = keyOf(session, turnId);
  return pageOnly.has(key) || typeof read()[key] === "number";
}

export function discardNotSent(session: string, turnId: string): void {
  const key = keyOf(session, turnId);
  try {
    const all = read();
    all[key] = Date.now();
    const kept = Object.entries(all)
      .sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0))
      .slice(0, LIMIT);
    localStorage.setItem(NOT_SENT_KEY, JSON.stringify(Object.fromEntries(kept)));
    pageOnly.delete(key);
  } catch {
    pageOnly.add(key);
  }
}

/** The row's state: the server's word first, then this tab's outbox, then Discard. A retry the
 *  server refused (4xx) records nothing, so Send again is offered again under the same id. */
export function notSentState(
  turn: StructuredTurn,
  outboxPhase: string | null,
  discarded: boolean,
): NotSentChoice | null {
  if (turn.not_sent?.retried || turn.not_sent?.retry_recorded) return "again";
  if (outboxPhase !== null && outboxPhase !== "refused") return "sending";
  return discarded ? "discarded" : null;
}

export type Attachment = NonNullable<StructuredTurn["attachments"]>[number];

/** Which pictures can no longer be read back. Only pictures can be asked (the read-back route
 *  serves images); a 404 means gone. Any other failure is not proof, so the server decides. */
export async function missingPictures(
  attachments: readonly Attachment[],
  probe: (stored: string) => Promise<unknown>,
): Promise<Attachment[]> {
  const pictures = attachments.filter((a) => a.mime.startsWith("image/"));
  const results = await Promise.all(
    pictures.map(async (a) => {
      try {
        await probe(a.stored);
        return null;
      } catch (e) {
        return (e as { status?: number } | null)?.status === 404 ? a : null;
      }
    }),
  );
  return results.filter((a): a is Attachment => a !== null);
}

/** "A picture is no longer available — send without it?", in the right number. */
export function missingPrompt(missing: number, nothingLeft: boolean): string {
  const lead = missing === 1 ? "A picture is no longer available" : `${missing} pictures are no longer available`;
  if (nothingLeft) return `${lead}, and nothing else is left to send.`;
  return `${lead} — send without ${missing === 1 ? "it" : "them"}?`;
}
