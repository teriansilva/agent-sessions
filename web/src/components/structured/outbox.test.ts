import { expect, test } from "vitest";
import type { StructuredSnapshot, StructuredTurn } from "../../types/api";
import {
  leavingNote,
  nextToPost,
  outboxEntry,
  reconcile,
  retried,
  revisionFor,
  unrecorded,
  updateEntry,
  withoutEntry,
  type OutboxEntry,
} from "./outbox";

const A = "aaaaaaaa-1111-4111-8111-111111111111";
const B = "bbbbbbbb-1111-4111-8111-111111111111";

function snap(over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: "codex-api:s",
    revision: 7,
    event_cursor: 7,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w", background_active: false },
    ...over,
  };
}

function turn(id: string, state = "queued"): StructuredTurn {
  return {
    turn_id: id, operation_id: id, state, text: "x", reply: "", text_truncated: false,
    reply_truncated: false, reason: null, tools: [], tools_truncated: false,
  };
}

const entry = (id: string, phase: OutboxEntry["phase"] = "waiting", recorded = false): OutboxEntry => ({
  ...outboxEntry({ id, text: `t-${id}`, draft: `t-${id} `, attachments: [{ name: "a.png", stored: "1-a.png" }], history: null }),
  phase,
  recorded,
});

test("an entry's request is frozen: a retry can only replay it", () => {
  const e = outboxEntry({ id: A, text: "hi", draft: "hi ", attachments: [{ name: "a.png", stored: "1-a.png" }], history: "h" });
  expect(e.phase).toBe("waiting");
  expect(Object.isFrozen(e.request)).toBe(true);
  expect(Object.isFrozen(e.request.attachments)).toBe(true);
  expect(Object.isFrozen(e.request.attachments[0])).toBe(true);
  expect(() => {
    (e.request as { id: string }).id = B;
  }).toThrow();
});

test("a conversation showing the turn drops its entry; an unchanged list is the same list", () => {
  const list = [entry(A, "sending"), entry(B)];
  expect(reconcile(list, [turn(A).turn_id]).map((e) => e.request.id)).toEqual([B]);
  expect(reconcile(list, [])).toBe(list);
});

test("a late result for an entry that is gone never brings it back", () => {
  const list = [entry(B)];
  expect(updateEntry(list, A, (e) => ({ ...e, phase: "refused" }))).toBe(list);
  expect(updateEntry(list, B, (e) => ({ ...e, phase: "sending" }))[0].phase).toBe("sending");
});

test("only a waiting, admitted head goes; a failed head stops the queue behind it", () => {
  expect(nextToPost([entry(A), entry(B)], true)?.request.id).toBe(A);
  expect(nextToPost([entry(A), entry(B)], false)).toBeNull(); // the client's admission says not now
  expect(nextToPost([entry(A, "sending"), entry(B)], true)).toBeNull();
  expect(nextToPost([entry(A, "refused"), entry(B)], true)).toBeNull();
  expect(nextToPost([entry(A, "unknown"), entry(B)], true)).toBeNull();
  expect(nextToPost([], true)).toBeNull();
});

test("Retry puts only an unknown entry back to waiting, under the same request", () => {
  const list = [entry(A, "unknown"), entry(B)];
  const next = retried(list, A);
  expect(next[0].phase).toBe("waiting");
  expect(next[0].request).toBe(list[0].request);
  expect(retried([entry(A, "refused")], A)[0].phase).toBe("refused");
});

test("Discard/Remove take an entry out, never one whose POST is out", () => {
  expect(withoutEntry([entry(A, "refused"), entry(B)], A).map((e) => e.request.id)).toEqual([B]);
  const sending = [entry(A, "sending")];
  expect(withoutEntry(sending, A)).toBe(sending);
});

test("the revision rides only when no turn is active", () => {
  expect(revisionFor(snap())).toBe(7);
  expect(revisionFor(snap({ state: "running", active_turn: A }))).toBeUndefined();
});

test("unrecorded and the leaving note follow the head's state", () => {
  expect(unrecorded([])).toBe(false);
  expect(leavingNote([])).toBeNull();
  expect(unrecorded([entry(A, "sending", true)])).toBe(false);
  expect(leavingNote([entry(A, "sending", true)])).toBeNull();
  const onItsWay = "Leaving drops this list; a message already on its way may still arrive";
  const notSent = "Not sent yet — leaving this session discards it";
  expect(leavingNote([entry(A, "sending"), entry(B)])).toBe(onItsWay);
  expect(leavingNote([entry(A, "unknown"), entry(B)])).toBe(onItsWay);
  expect(leavingNote([entry(A)])).toBe(notSent);
  expect(leavingNote([entry(A, "refused"), entry(B)])).toBe(notSent);
  expect(leavingNote([entry(A, "sending", true), entry(B)])).toBe(notSent);
});
