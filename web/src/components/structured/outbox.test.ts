import { describe, expect, test } from "vitest";
import type { StructuredSnapshot, StructuredTurn } from "../../types/api";
import {
  conversationReading,
  leavingNote,
  leftForRetry,
  mayResend,
  nextToPost,
  outboxEntry,
  reconcile,
  recovered,
  renewed,
  resendStands,
  retried,
  revisionFor,
  recoveredOver,
  sameConversation,
  stopCapture,
  stopStands,
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

test("Send again after a known refusal is a NEW operation with the same words, in place (#1733)", () => {
  const C = "cccccccc-1111-4111-8111-111111111111";
  const refused: OutboxEntry = {
    ...outboxEntry({ id: A, text: "continue", draft: "continue ", attachments: [{ name: "a.png", stored: "s" }], history: "h" }),
    phase: "refused",
    unwritable: true,
    reason: "BattleLab couldn't write this session's record (disk full); nothing was sent",
  };
  const waiting = outboxEntry({ id: B, text: "next", draft: "next", attachments: [], history: null });
  const list = renewed([refused, waiting], A, C);
  expect(list.map((e) => e.request.id)).toEqual([C, B]);
  expect(list[0].phase).toBe("waiting");
  expect(list[0].unwritable).toBeUndefined();
  expect(list[0].request.text).toBe("continue");
  expect(list[0].request.draft).toBe("continue ");
  expect(list[0].request.attachments).toEqual([{ name: "a.png", stored: "s" }]);
  // Never for an unknown entry: it may have been recorded, so only its own id may go again.
  const unknown: OutboxEntry = { ...refused, phase: "unknown" };
  expect(renewed([unknown], A, C)).toEqual([unknown]);
});

// --- #1420, #1725 -----------------------------------------------------------------------------

const settled = (over: Partial<StructuredTurn> = {}): StructuredTurn =>
  ({ turn_id: A, operation_id: A, state: "completed", text: "hi", reply: "there", tools: [], ...over }) as StructuredTurn;

test("sameConversation: a worker's last records move the revision and nothing the operator reads", () => {
  const seen = snap({ revision: 12, turns: [settled()] });
  const now = snap({ revision: 14, event_cursor: 14, turns: [settled()], native: { native_id: "n", worker: null, background_active: false } });
  expect(sameConversation(seen, now)).toBe(true);
});

test.each<[string, Partial<StructuredSnapshot>]>([
  ["a new turn", { turns: [settled(), settled({ turn_id: B, operation_id: B })] }],
  ["a changed reply", { turns: [settled({ reply: "more" })] }],
  ["a turn that read differently", { turns: [settled({ state: "uncertain" })] }],
  ["a new request", {
    turns: [settled()],
    pending_requests: [{ request_id: "r", turn_id: A, kind: "command", choices: ["reject"] }],
  }],
  ["a running turn", { turns: [settled()], active_turn: A }],
  ["an agent turn starting", {
    turns: [settled()],
    native: { native_id: "n", worker: "w", background_active: true, background_turn: B },
  }],
])("sameConversation: %s is a real change", (_name, over) => {
  expect(sameConversation(snap({ turns: [settled()] }), snap(over))).toBe(false);
});

test("mayResend: only once, and only with nothing running or asked", () => {
  const seen = snap({ turns: [settled()] });
  const entry = outboxEntry({ id: A, text: "t", draft: "t", attachments: [], history: null });
  expect(mayResend(entry, seen, snap({ revision: 9, turns: [settled()] }))).toBe(true);
  expect(mayResend({ ...entry, resent: true }, seen, snap({ turns: [settled()] }))).toBe(false);
  expect(mayResend(entry, seen, null)).toBe(false); // the read failed: nothing proves anything
  expect(mayResend(entry, seen, snap({ state: "running", active_turn: A, turns: [settled()] }))).toBe(false);
  expect(mayResend(entry, seen, snap({ turns: [settled(), settled({ turn_id: B })] }))).toBe(false);
  expect(
    mayResend(entry, seen, snap({
      turns: [settled()],
      pending_requests: [{ request_id: "r", turn_id: A, kind: "command", choices: ["reject"] }],
    })),
  ).toBe(false);
});

test("recovered: a refused or unknown entry goes back to waiting under the SAME id, its refusal gone", () => {
  const entry: OutboxEntry = {
    ...outboxEntry({ id: A, text: "t", draft: "t", attachments: [], history: null }),
    phase: "refused",
    reason: "the conversation changed",
    remedy: "retry",
  };
  const [back] = recovered([entry], A);
  expect(back.request.id).toBe(A);
  expect(back.phase).toBe("waiting");
  expect(back.reason).toBeUndefined();
  expect(back.remedy).toBeUndefined();
  // Never one on its way, and a refused head is never advanced by anything but this.
  const sending: OutboxEntry = { ...entry, phase: "sending" };
  expect(recovered([sending], A)[0].phase).toBe("sending");
  expect(nextToPost([entry, outboxEntry({ id: B, text: "b", draft: "b", attachments: [], history: null })], true)).toBeNull();
});

test("resendStands: a scheduled resend goes only over the conversation it was scheduled over", () => {
  const seen = snap({ revision: 12, turns: [settled()] });
  const plain = outboxEntry({ id: A, text: "t", draft: "t", attachments: [], history: null });
  // An ordinary send and the operator's own Retry carry no fence.
  expect(resendStands(plain, snap({ turns: [settled(), settled({ turn_id: B })] }))).toBe(true);
  const fenced: OutboxEntry = { ...plain, resent: true, resendFence: conversationReading(seen) };
  expect(resendStands(fenced, snap({ revision: 14, event_cursor: 14, turns: [settled()] }))).toBe(true);
  expect(resendStands(fenced, snap({ turns: [settled(), settled({ turn_id: B, operation_id: B })] }))).toBe(false);
  expect(resendStands(fenced, snap({ turns: [settled({ reply: "more" })] }))).toBe(false);
  // Retry clears it: the operator has read what changed.
  const refused: OutboxEntry = { ...fenced, phase: "refused" };
  expect(recovered([refused], A)[0].resendFence).toBeUndefined();
});

test("sameConversation: the plan is what the agent says it will do, so changing it is a real change", () => {
  const live = { native_id: "n", worker: "w", background_active: false };
  const plan = { entries: [{ text: "one", status: "pending" as const }], entries_at: 5 };
  const seen = snap({ turns: [settled()], native: { ...live, plan } });
  expect(sameConversation(seen, snap({ turns: [settled()], native: { ...live, plan } }))).toBe(true);
  expect(
    sameConversation(seen, snap({ turns: [settled()], native: {
      ...live, plan: { entries: [{ text: "one", status: "completed" as const }], entries_at: 6 } } })),
  ).toBe(false);
  expect(
    sameConversation(seen, snap({ turns: [settled()], native: { ...live, plan: { ...plan, document: "The plan." } } })),
  ).toBe(false);
  // Marked out of date, or restated at a later time, it says nothing new (the server's own rule).
  expect(
    sameConversation(seen, snap({ turns: [settled()], native: { ...live, plan: { ...plan, stale: true, entries_at: 9 } } })),
  ).toBe(true);
  // What the client reports it works with is no part of the conversation.
  expect(
    sameConversation(seen, snap({ turns: [settled()], native: { ...live, plan, environment: { mcp: [{ name: "x", status: "ready" }] } } as never })),
  ).toBe(true);
});

describe("Stop and resume attributes every request it can't see any more", () => {
  const request = (id = "r") => ({ request_id: id, turn_id: B, kind: "Bash", choices: ["approve"],
    background_task: { task_id: "t", description: "d" } });
  const live = { native_id: "n", worker: "w", background_active: true, background_turn: null };
  const gone = { native_id: "n", worker: null, background_active: false };
  const ended = (id = "r") => ({ worker: "w", turn_id: B, request_id: id });
  const before = (pending = [request()]) => snap({ turns: [settled()], pending_requests: pending, native: live });
  const after = (over: Partial<StructuredSnapshot> = {}, native: object = {}) =>
    snap({ revision: 20, event_cursor: 20, turns: [settled()], pending_requests: [], ...over,
      native: { ...gone, ...native } });

  test("the requests the Stop retired are exactly those open when it was pressed: it resumes", () => {
    expect(stopStands(stopCapture(before()), after({}, { ended_requests: [ended()] }))).toBe(true);
    expect(stopStands(stopCapture(before([])), after())).toBe(true);
  });

  test("another tab decided one before the Stop: not attributable", () => {
    expect(stopStands(stopCapture(before()), after())).toBe(false);
    expect(stopStands(stopCapture(before([request("a"), request("b")])), after({}, { ended_requests: [ended("a")] }))).toBe(false);
  });

  test("a request arrived on its own: not attributable", () => {
    // Still open after the Stop, or retired by it without ever having been shown.
    expect(stopStands(stopCapture(before([])), after({ pending_requests: [request()] }))).toBe(false);
    expect(stopStands(stopCapture(before([])), after({}, { ended_requests: [ended()] }))).toBe(false);
    expect(stopStands(stopCapture(before()), after({}, { ended_requests: [ended(), ended("x")] }))).toBe(false);
  });

  test("only what that generation's Stop retired counts", () => {
    const other = { ...ended(), worker: "older" };
    // An older generation's retired request is history, whatever it is called.
    expect(stopStands(stopCapture(before([])), after({}, { ended_requests: [other] }))).toBe(true);
    expect(stopStands(stopCapture(before()), after({}, { ended_requests: [other] }))).toBe(false);
    // A server that doesn't say can't attribute one.
    expect(stopStands(stopCapture(before()), after())).toBe(false);
  });

  test("the conversation must be unchanged, and the generation must be over", () => {
    const capture = stopCapture(before([]));
    expect(stopStands(capture, after({ turns: [settled(), settled({ turn_id: B, operation_id: B })] }))).toBe(false);
    expect(stopStands(capture, after({ turns: [settled({ state: "uncertain" })] }))).toBe(false);
    expect(stopStands(capture, snap({ revision: 20, turns: [settled()], native: live }))).toBe(false);
  });
});

test("recoveredOver: back to waiting under the same id, fenced to what was accepted", () => {
  const refused: OutboxEntry = {
    ...outboxEntry({ id: A, text: "t", draft: "t", attachments: [], history: null }),
    phase: "refused",
    reason: "stop first",
    remedy: "stop_resume",
  };
  const shown = snap({ revision: 20, turns: [settled()] });
  const [entry] = recoveredOver([refused], A, shown);
  expect(entry.phase).toBe("waiting");
  expect(entry.request.id).toBe(A);
  expect(entry.remedy).toBeUndefined();
  expect(resendStands(entry, shown)).toBe(true);
  expect(resendStands(entry, snap({ turns: [settled(), settled({ turn_id: B })] }))).toBe(false);
});

test("leftForRetry: never moves the entry, and the way on is the operator's Retry", () => {
  const refused: OutboxEntry = {
    ...outboxEntry({ id: A, text: "t", draft: "t", attachments: [], history: null }),
    phase: "refused",
    remedy: "stop_resume",
  };
  const [entry] = leftForRetry([refused], A);
  expect(entry.phase).toBe("refused");
  expect(entry.remedy).toBe("retry");
  expect(nextToPost([entry], true)).toBeNull();
});

test("an operator's Retry clears a fence: they have read what changed", () => {
  const fenced: OutboxEntry = { ...entry(A, "unknown"), resent: true, resendFence: "old" };
  expect(retried([fenced], A)[0].resendFence).toBeUndefined();
});
