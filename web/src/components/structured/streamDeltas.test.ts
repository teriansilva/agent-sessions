/** #1727: the pane builds a streaming reply from the journal's text events exactly as the server's
 *  projection does. `streamDeltas.fixture.json` is real server output (rebuilt and compared by
 *  tests/test_stream_deltas_fixture.py): a snapshot mid-stream, the events page after its cursor,
 *  and the snapshot taken after those events. */
import { expect, test } from "vitest";

import type { StructuredEventPage, StructuredSnapshot, StructuredTurn } from "../../types/api";
import fixture from "./streamDeltas.fixture.json";
import { applyTextDeltas } from "./streamDeltas";
import { messageTimestamp } from "./structuredView";

type Case = {
  applies: boolean;
  base: Partial<StructuredSnapshot>;
  page: Omit<StructuredEventPage, "session_key">;
  next: Partial<StructuredSnapshot>;
};
const cases = fixture as unknown as Record<string, Case>;

const snap = (s: Partial<StructuredSnapshot>) => s as StructuredSnapshot;
const page = (p: Case["page"]) => ({ session_key: "k", ...p }) as StructuredEventPage;
const view = (t: StructuredSnapshot["turns"][number]) => {
  const { turn_id, state, reply, reply_breaks, stream } = t as StructuredTurn;
  return { turn_id, state, reply, reply_breaks, stream };
};

for (const [name, c] of Object.entries(cases)) {
  test(`${name}: text rebuilt from the events ${c.applies ? "equals the next snapshot's" : "is left to a snapshot"}`, () => {
    const out = applyTextDeltas(snap(c.base), page(c.page));
    if (!c.applies) {
      expect(out).toBeNull();
      return;
    }
    expect(out).not.toBeNull();
    expect(out!.turns.map(view)).toEqual(c.next.turns!.map(view));
    // It carries the page's cursor: an older snapshot read in flight can never replace it.
    expect(out!.event_cursor).toBe(c.page.next_cursor);
    // …while its revision is still the snapshot's: no fact but the text changed.
    expect(out!.revision).toBe(c.base.revision);
  });
}

const base = cases["unnamed@1"];

test("a page that does not start right after the held snapshot's cursor is never applied", () => {
  // The same page against a snapshot one record later (a newer snapshot arrived meanwhile).
  const later = snap({ ...base.base, event_cursor: base.base.event_cursor! + 1 });
  expect(applyTextDeltas(later, page(base.page))).toBeNull();
  // …nor applied twice: the result's own cursor is past the page.
  const once = applyTextDeltas(snap(base.base), page(base.page))!;
  expect(applyTextDeltas(once, page(base.page))).toBeNull();
});

test("a page short of the journal's head is never shown half-applied", () => {
  const short = { ...base.page, revision: base.page.next_cursor + 3 };
  expect(applyTextDeltas(snap(base.base), page(short))).toBeNull();
});

test("text naming another turn (a retry is a new operation) or flagged truncated is a snapshot's", () => {
  const retry = {
    ...base.page,
    events: base.page.events.map((e) => ({ ...e, data: { ...e.data, operation_id: "66666666-2222-4333-8444-555555555555" } })),
  };
  expect(applyTextDeltas(snap(base.base), page(retry))).toBeNull();
  const truncated = { ...base.page, events: base.page.events.map((e) => ({ ...e, data: { ...e.data, truncated: true } })) };
  expect(applyTextDeltas(snap(base.base), page(truncated))).toBeNull();
});

test("only a running turn the snapshot marks as streaming is extended", () => {
  const ended = snap({ ...base.base, turns: base.base.turns!.map((t) => ({ ...t, state: "completed" })) });
  expect(applyTextDeltas(ended, page(base.page))).toBeNull();
  const unmarked = snap({
    ...base.base,
    turns: base.base.turns!.map((t) => ({ ...(t as StructuredTurn), stream: undefined })),
  });
  expect(applyTextDeltas(unmarked, page(base.page))).toBeNull();
});

test("a reply that starts between snapshots takes its time from the page, or shows none", () => {
  const empty = cases["unnamed@0"]; // the reply was "" when the snapshot was taken
  const at = (out: StructuredSnapshot | null) => (out!.turns[0] as StructuredTurn).reply_ts;
  const startedNull = snap({ ...empty.base, turns: empty.base.turns!.map((t) => ({ ...t, reply_ts: null })) });
  // The operator's pages carry no event time today: the reply's time stays null, and the
  // header's formatter turns null into no time at all — never "Invalid Date" or 1970.
  const out = applyTextDeltas(startedNull, page(empty.page));
  expect(out!.turns[0].reply).toBe("Sure, here it is:\n\n- one\n- two");
  expect(at(out)).toBeNull();
  expect(messageTimestamp(at(out), "assistant")).toBeNull();
  // A page that does carry it: the event that started the reply sets it, once.
  const timed = { ...empty.page, events: empty.page.events.map((e, i) => ({ ...e, ts: 1_790_000_100 + i })) };
  expect(at(applyTextDeltas(startedNull, page(timed)))).toBe(1_790_000_100);
  // A reply already started keeps its own time.
  const started = snap({ ...base.base, turns: base.base.turns!.map((t) => ({ ...t, reply_ts: 1_790_000_000 })) });
  const later = { ...base.page, events: base.page.events.map((e) => ({ ...e, ts: 1_790_000_500 })) };
  expect(at(applyTextDeltas(started, page(later)))).toBe(1_790_000_000);
});
