/** The queue behind a running HQ turn (#2103), as the console needs it: pure derivations over
 *  the turns on screen. The server is the authority on admission (a full queue is its 409); this
 *  only decides what the composer offers and what is marked seen. */

/** How many questions may wait behind a conversation's running turn
 *  (`ask_orchestrator.QUEUE_MAX`; `tests/test_hq_tasks.py` pins the two against each other). */
export const ASK_QUEUE_MAX = 3;

/** A turn that waits or never ran, by its server status; `null` for one that ran or runs. */
export type AskQueueState = "queued" | "withdrawn" | "not_started" | null;

export function queueStateOf(status: string): AskQueueState {
  return status === "queued" || status === "withdrawn" || status === "not_started"
    ? status
    : null;
}

interface QueueTurn {
  queue?: AskQueueState;
  pending: boolean;
  error: string | null;
}

/** How many questions are waiting in the conversation on screen. */
export function queuedCount(turns: readonly QueueTurn[]): number {
  return turns.filter((t) => t.queue === "queued").length;
}

/** What the composer offers while a turn runs: nothing special (`idle`: no turn runs), a Send
 *  that queues (`open`), or a disabled Send because the queue is full (`full`). */
export function queueOffer(turns: readonly QueueTurn[]): "idle" | "open" | "full" {
  if (!turns.some((t) => t.pending && !t.error)) return "idle";
  return queuedCount(turns) >= ASK_QUEUE_MAX ? "full" : "open";
}

/** The quiet line under the composer, or none. */
export function queueLine(turns: readonly QueueTurn[]): { text: string; warn: boolean } | null {
  const offer = queueOffer(turns);
  if (offer === "full") return { text: "Queue full — withdraw one or wait", warn: true };
  if (offer === "open" && queuedCount(turns) > 0)
    return { text: "Queued behind the running question", warn: false };
  return null;
}

/** The words under a question that waits or never ran. */
export function queueLabel(state: Exclude<AskQueueState, null>): string {
  if (state === "queued") return "Queued";
  if (state === "withdrawn") return "Withdrawn";
  return "Not started — the server restarted";
}

/** What is SEEN is what is on screen: the newest followed-session update shown and the newest
 *  turn SETTLEMENT shown. A turn that is still queued or running contributes nothing, so having
 *  seen a question wait never acknowledges its later answer. */
export function shownThrough(
  updates: readonly { ts: number }[],
  turns: readonly { settledAt?: number | null }[],
): number {
  let through = 0;
  for (const u of updates) through = Math.max(through, u.ts);
  for (const t of turns) if (typeof t.settledAt === "number") through = Math.max(through, t.settledAt);
  return through;
}

/** Whether there is something on screen the server has not been told was seen. */
export function seenIsOwed(through: number, seenAt: number, unread: boolean): boolean {
  return through > 0 && (unread || through > seenAt);
}
