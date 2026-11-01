import type { StructuredSnapshot } from "../../types/api";
import { isActive } from "./structuredView";

/** What a refusal names as its way on (#1725), from the server's closed `remedy`. `retry`: the same
 *  operation id goes again. `stop_resume`, `resume_with_attachments`: the confirmed Stop first,
 *  then the same id. No new route: each goes through the checks that route already has. */
export type Remedy = "retry" | "stop_resume" | "resume_with_attachments";

/** The composer's outbox (#1465). Send moves the composer's message here at once, as ONE frozen
 *  request with its own operation id, so the composer is free for the next message. Entries go
 *  out FIFO, only the head in flight, and each leaves the outbox the moment a snapshot shows its
 *  turn (`turn_id === id`), whatever its POST says. Tab-local by design: nothing is persisted. */

/** What a send carries, frozen at Send: a retry replays exactly this (same id, text, uploads). */
export type OutboxRequest = Readonly<{
  /** The operation id, minted at Send, which is also the recorded turn's id. */
  id: string;
  /** What is sent: the trimmed text. */
  text: string;
  /** What was typed, untrimmed, for Edit. */
  draft: string;
  attachments: readonly Readonly<{ name: string; stored: string }>[];
  /** Its sent-history entry: one per operation, confirmed once the server has the turn. */
  history: string | null;
}>;

/** `waiting` — not posted yet; `sending` — its POST is out (or answered, see `recorded`);
 *  `refused` — the server said no (4xx), nothing was recorded; `unknown` — no answer came back,
 *  so it may have been recorded. Refused and unknown stop the queue behind them. */
export type OutboxPhase = "waiting" | "sending" | "refused" | "unknown";

export type OutboxEntry = Readonly<{
  request: OutboxRequest;
  phase: OutboxPhase;
  /** The POST came back 2xx: the server has it, the conversation will show it on the next read. */
  recorded: boolean;
  /** The journal revision the 2xx answer reported, read after the turn was recorded: a snapshot
   *  at or past it has the turn even when it no longer lists it (it aged out of the window). */
  receipt?: number;
  /** A refusal's reason, from the server. */
  reason?: string;
  /** #1733: refused because BattleLab could not write the session's record (409
   *  `journal_unwritable`). Known: nothing was claimed. The draft stays here; Send again is a NEW
   *  send (a new operation id), never a Retry. */
  unwritable?: boolean;
  /** The action its refusal names (#1725), offered as a button. */
  remedy?: Remedy;
  /** #1420: it already went again once by itself, after a revision that moved under nothing the
   *  operator could see. Never twice: a second refusal is the operator's to read. */
  resent?: boolean;
  /** #1420: the conversation shown when that resend was scheduled. It goes out only over exactly
   *  this: anything newer on screen is the operator's to read first. */
  resendFence?: string;
}>;

export function outboxEntry(request: Omit<OutboxRequest, "attachments"> & {
  attachments: readonly { name: string; stored: string }[];
}): OutboxEntry {
  const frozen: OutboxRequest = Object.freeze({
    ...request,
    attachments: Object.freeze(request.attachments.map((a) => Object.freeze({ name: a.name, stored: a.stored }))),
  });
  return Object.freeze({ request: frozen, phase: "waiting", recorded: false });
}

/** Drop every entry the conversation proves recorded: it lists a turn with the entry's id, or (for
 *  an entry holding a 2xx receipt) the read's revision is at or past that receipt — the snapshot is
 *  bounded, so a completed turn can age out of it before a failed read recovers. Called only with
 *  a read that succeeded: a failed read changes nothing. Both clients key a turn by the id the
 *  browser minted (structured `turn_id === operation_id`, chat `turn_id`). */
export function reconcile(
  list: readonly OutboxEntry[],
  recorded: Iterable<string>,
  revision?: number,
): OutboxEntry[] {
  const turns = new Set(recorded);
  const kept = list.filter((e) => !turns.has(e.request.id) &&
    !(revision !== undefined && e.receipt !== undefined && revision >= e.receipt));
  return kept.length === list.length ? (list as OutboxEntry[]) : kept;
}

/** Replace one entry by id. An entry that is gone (reconciled, discarded, its pane switched)
 *  stays gone: a late result never brings it back. */
export function updateEntry(
  list: readonly OutboxEntry[],
  id: string,
  change: (e: OutboxEntry) => OutboxEntry,
): OutboxEntry[] {
  return list.some((e) => e.request.id === id)
    ? list.map((e) => (e.request.id === id ? Object.freeze(change(e)) : e))
    : (list as OutboxEntry[]);
}

/** Retry an unknown entry: back to waiting with the SAME request (id, text, uploads), so the server
 *  replays it if it has it; the pump posts it under the client's admission rule. */
export function retried(list: readonly OutboxEntry[], id: string): OutboxEntry[] {
  return updateEntry(list, id, (e) =>
    e.phase === "unknown" ? { ...e, phase: "waiting", resendFence: undefined } : e,
  );
}

/** A refusal's way on (#1725): back to waiting with the SAME request, so the server replays it if it
 *  has it. Only the operator's press or the remedy's own Stop gets here, never the queue itself. */
export function recovered(list: readonly OutboxEntry[], id: string): OutboxEntry[] {
  return updateEntry(list, id, (e) =>
    e.phase === "refused" || e.phase === "unknown"
      ? { ...e, phase: "waiting", reason: undefined, remedy: undefined, resendFence: undefined }
      : e,
  );
}

/** Stop and resume, once Stop has proved the worker gone (#1725): back to waiting, the same id, and
 *  fenced to the read the pane accepted after it, like the automatic resend. */
export function recoveredOver(
  list: readonly OutboxEntry[],
  id: string,
  shown: StructuredSnapshot,
): OutboxEntry[] {
  const fence = conversationReading(shown);
  return updateEntry(list, id, (e) =>
    e.phase === "refused" || e.phase === "unknown"
      ? { ...e, phase: "waiting", reason: undefined, remedy: undefined, resendFence: fence }
      : e,
  );
}

/** Stop and resume when Stop did its part and the conversation can't be vouched for (it changed,
 *  or the read after it failed or lost a race): the operator's to read, then to Retry. */
export function leftForRetry(list: readonly OutboxEntry[], id: string): OutboxEntry[] {
  return updateEntry(list, id, (e) =>
    e.phase === "refused" || e.phase === "unknown"
      ? {
          ...e,
          reason: "the session stopped, but the conversation changed or couldn’t be read, so this wasn’t sent",
          remedy: "retry",
        }
      : e,
  );
}

/** Is what the operator was looking at still what the conversation holds (#1420)? A worker's last
 *  records (a stop, an idle exit, a crash) move the revision and change nothing here. A completed
 *  turn, a new request, an agent turn or another message does: that is a real conflict, however
 *  idle the session is now. Everything the operator reads of a turn counts. */
export function sameConversation(seen: StructuredSnapshot, now: StructuredSnapshot): boolean {
  return conversationReading(seen) === conversationReading(now);
}

export function conversationReading(s: StructuredSnapshot): string {
  // The plan is what the agent says it will do, so the operator may be answering it (#1726). When
  // it was recorded, and being marked out of date, say nothing new: the server's own rule.
  const plan = s.native?.plan;
  return JSON.stringify([
    s.turns,
    s.pending_requests,
    s.active_turn,
    s.native?.background_turn ?? null,
    plan?.entries ?? null,
    plan?.explanation ?? null,
    plan?.document ?? null,
  ]);
}

/** What Stop and resume was pressed over (#1725). */
export interface StopCapture {
  worker: string | null;
  requests: string[];
  reading: string;
}

const requestKeys = (rows: { turn_id: string; request_id: string }[]) =>
  rows.map((row) => JSON.stringify([row.turn_id, row.request_id])).sort();

/** The conversation apart from what is attributed one by one: a Stop always ends the agent's own
 *  turn target, and a request it ended is compared by identity. A turn it cut reads differently,
 *  and the operator reads that first. */
const readingAcrossStop = (s: StructuredSnapshot) =>
  conversationReading({
    ...s,
    pending_requests: [],
    native: s.native ? { ...s.native, background_turn: null } : s.native,
  });

export function stopCapture(s: StructuredSnapshot): StopCapture {
  return {
    worker: s.native?.worker ?? null,
    requests: requestKeys(s.pending_requests),
    reading: readingAcrossStop(s),
  };
}

/** May the held message go by itself after its Stop? Only if every request is attributable: those
 *  the Stop retired are exactly those open when it was pressed, for the generation it ended, and
 *  none is open now. A request another tab decided and one the Stop ended both leave
 *  `pending_requests`, so that alone proves nothing; one arriving by itself mustn't be masked. */
export function stopStands(captured: StopCapture, after: StructuredSnapshot): boolean {
  const ended = (after.native?.ended_requests ?? []).filter((row) => row.worker === captured.worker);
  return (
    captured.reading === readingAcrossStop(after) &&
    after.pending_requests.length === 0 &&
    (after.native?.worker ?? null) !== captured.worker &&
    JSON.stringify(requestKeys(ended)) === JSON.stringify(captured.requests)
  );
}

/** A scheduled resend at the moment it would post (#1420): does the pane still show what it was
 *  scheduled over? An ordinary send, or the operator's own Retry, has no fence. */
export function resendStands(entry: OutboxEntry, shown: StructuredSnapshot): boolean {
  return entry.resendFence === undefined || entry.resendFence === conversationReading(shown);
}

/** May a message refused for a moved revision go again by itself (#1420)? Once, only with nothing
 *  running or asked and the conversation unchanged. The same operation id, so the server replays it
 *  if it has it; it can only re-send what the operator already pressed Send on. ``now`` must be
 *  what the pane ACCEPTED as current: a read older than one already shown proves nothing. */
export function mayResend(
  entry: OutboxEntry,
  seen: StructuredSnapshot,
  now: StructuredSnapshot | null,
): boolean {
  return (
    !entry.resent &&
    !!now &&
    !isActive(now) &&
    now.pending_requests.length === 0 &&
    sameConversation(seen, now)
  );
}

/** Send again after a KNOWN refusal (#1733: the session's record could not be written, and nothing
 *  was claimed): the same words and pictures as a NEW operation, `nextId`, back to waiting in the
 *  same place (`nextId === id` re-sends under the never-claimed refused id, when this device's
 *  sent history cannot be pointed at a new one). Only a refused entry — an unknown one may have
 *  been recorded, so it is only ever retried under its own id. */
export function renewed(list: readonly OutboxEntry[], id: string, nextId: string): OutboxEntry[] {
  if (!list.some((e) => e.request.id === id && e.phase === "refused")) return list as OutboxEntry[];
  return list.map((e) =>
    e.request.id === id && e.phase === "refused" ? outboxEntry({ ...e.request, id: nextId }) : e,
  );
}

/** Discard (refused/unknown) or Remove (waiting): out of the queue, which continues in order. A
 *  message whose POST is out cannot be taken back. */
export function withoutEntry(list: readonly OutboxEntry[], id: string): OutboxEntry[] {
  const kept = list.filter((e) => e.request.id !== id || e.phase === "sending");
  return kept.length === list.length ? (list as OutboxEntry[]) : kept;
}

/** The entry to post now, or null. Only the head goes, only while it is waiting: a refused or
 *  unknown head holds everything behind it until the operator retries, edits or discards it.
 *  `admitted` is the client's own admission rule, read from its latest conversation read: a native
 *  worker queues server-side (anything but read only / a permission change / an unstarted skip
 *  session); the HTTP chat client takes one reply at a time (nothing in flight). */
export function nextToPost(list: readonly OutboxEntry[], admitted: boolean): OutboxEntry | null {
  const head = list[0];
  return head && head.phase === "waiting" && admitted ? head : null;
}

/** `expected_revision` rides only when no turn is active (#1378): streaming observations keep
 *  advancing the revision, and a queued message binds its operation id instead. Read from the
 *  snapshot current at POST time, so a queued entry always carries a fresh revision. */
export function revisionFor(snap: StructuredSnapshot): number | undefined {
  return isActive(snap) ? undefined : snap.revision;
}

/** Anything the server does not have yet: what a reload or close would lose. */
export function unrecorded(list: readonly OutboxEntry[]): boolean {
  return list.some((e) => !e.recorded);
}

/** The note under the pending block, honest for every way out of the session. */
export function leavingNote(list: readonly OutboxEntry[]): string | null {
  if (!unrecorded(list)) return null;
  const head = list[0];
  return head && ((head.phase === "sending" && !head.recorded) || head.phase === "unknown")
    ? "Leaving drops this list; a message already on its way may still arrive"
    : "Not sent yet — leaving this session discards it";
}
