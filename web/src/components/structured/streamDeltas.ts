/** A streaming reply between snapshots (#1727): the journal's own text events, applied to the one
 *  turn the snapshot says is streaming.
 *
 *  The worker journals a running turn's text every 200 ms; a snapshot costs a projection of the
 *  whole conversation (≈0.5 s on a large one). So while a turn streams, the pane reads the events
 *  page after its snapshot's cursor and extends that turn's reply itself — with exactly the rules
 *  the server's projection uses (`native_runtime._append` / `_paragraph`), so the text it builds
 *  is the text the next snapshot shows. Anything else the page holds is the snapshot's to show.
 *
 *  The ordering fence:
 *  - a page applies only to the snapshot it was read after: its events start right after that
 *    snapshot's `event_cursor`, with no record between them (cursors count EVERY journal record;
 *    a gap is a private record, e.g. a handoff, that changes what the snapshot would show);
 *  - only partial text events of the turn the snapshot marks `stream`, still running, whose
 *    operation id is that turn's own — a retry is a new operation, so it never splices in. The
 *    marked text may be the turn's settled messages plus the one streaming now (#1727 part 2):
 *    the events extend that message (`item`); one that settles is read from the next snapshot;
 *  - the page must reach the journal's head, so what is shown is never a partial page;
 *  - the result carries the page's cursor as its `event_cursor` (its `revision` stays the
 *    snapshot's), so an older snapshot read in flight cannot replace it, and the next snapshot
 *    replaces the text wholesale (and becomes the new base).
 *  Returns null when the page cannot be applied: the caller reads a snapshot instead. */
import type { StructuredEventPage, StructuredSnapshot, StructuredTurn } from "../../types/api";

type Event = StructuredEventPage["events"][number];

/** Python's `str.strip()` emptiness, close enough for "a block with no visible text". */
const blank = (s: string) => s.trim() === "";

function streamingTurn(t: StructuredSnapshot["turns"][number]): t is StructuredTurn {
  return t.kind !== "agent" && t.state === "running" && !!(t as StructuredTurn).stream;
}

/** The snapshot holds a turn whose reply its text events may extend. */
export function streaming(snap: StructuredSnapshot): boolean {
  return snap.turns.some(streamingTurn);
}

/** One partial text delta for `turnId`, or null if this event is anything else. */
function delta(e: Event, turnId: string): { text: string; item: string | null } | null {
  const d = e.data;
  if (e.kind !== "text" || !d) return null;
  if (d.partial !== true || d.truncated === true || d.questions || "agent_turn" in d) return null;
  if (d.operation_id !== turnId || typeof d.text !== "string") return null;
  const item = typeof d.item_id === "string" && d.item_id ? d.item_id : null;
  return { text: d.text, item };
}

export function applyTextDeltas(
  snap: StructuredSnapshot,
  page: StructuredEventPage,
): StructuredSnapshot | null {
  const events = page.events;
  if (!events.length || page.next_cursor !== page.revision) return null;
  // Contiguous from the snapshot's cursor: no record (event or private) between them.
  if (events.some((e, i) => e.cursor !== snap.event_cursor + 1 + i)) return null;
  if (events[events.length - 1].cursor !== page.next_cursor) return null;
  const at = snap.turns.findIndex(streamingTurn);
  if (at < 0) return null;
  const turn = snap.turns[at] as StructuredTurn;
  let reply = turn.reply ?? "";
  let item = turn.stream!.item;
  // When the reply began (the first non-empty text, as the projection's `reply_ts`): taken from
  // the event that starts it when the page carries its time. Otherwise it stays null — the
  // header then shows no time (never a made-up one) until the next snapshot says.
  let replyTs = turn.reply_ts ?? null;
  const breaks = turn.reply_breaks ? [...turn.reply_breaks] : undefined;
  for (const e of events) {
    const d = delta(e, turn.turn_id);
    if (!d) return null;
    if (replyTs === null && d.text && typeof e.ts === "number" && Number.isFinite(e.ts)) replyTs = e.ts;
    if (d.item && item && d.item !== item) {
      // A new message: a paragraph apart (`_paragraph`), and its start recorded.
      if (!blank(d.text)) {
        if (reply && !reply.endsWith("\n\n")) reply += "\n\n";
        reply += d.text;
        breaks?.push(reply.length - d.text.length);
      }
    } else {
      reply += d.text; // more of the message in progress
    }
    // The first text of a reply starts its first message (the server's `reply_breaks` [0]).
    if (breaks && reply && !breaks.length) breaks.push(0);
    item = d.item ?? item;
  }
  const next: StructuredTurn = {
    ...turn,
    reply,
    reply_ts: replyTs,
    stream: { item },
    ...(breaks ? { reply_breaks: breaks } : {}),
  };
  const turns = [...snap.turns];
  turns[at] = next;
  // `revision` stays the snapshot's: the facts it projects (and what keys off it, like Always send
  // now's retries) are that snapshot's. `event_cursor` says how far its text now reaches.
  return { ...snap, turns, event_cursor: page.next_cursor };
}
