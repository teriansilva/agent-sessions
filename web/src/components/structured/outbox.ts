import type { StructuredSnapshot } from "../../types/api";
import { isActive } from "./structuredView";

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
  return updateEntry(list, id, (e) => (e.phase === "unknown" ? { ...e, phase: "waiting" } : e));
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
