/** Working together on a shared session (#2062), as pure mapping: the owner's strip, an access
 *  request's states, a decision card per level and per server code, the notes reducer (keyed by
 *  grant, with its write states) and the Continue-here stage machine. What must never happen is
 *  provable here: a widening option enabled for a peer, a draft or a pending write crossing to
 *  another grant's thread, a session created from anything but the preview's Start session. */
import { describe, expect, test } from "vitest";
import { choiceLabel } from "../components/structured/structuredView";
import type { StructuredRequest } from "../types/api";
import type { FederationGrant } from "./federation";
import { writeFailure } from "./federation";
import {
  CONTINUE_EXPIRED,
  CONTINUE_UNCONFIRMED,
  accessFailure,
  accessStatusIsOlder,
  answerSignature,
  attemptAnswer,
  continueCreateFailure,
  lockedAnswer,
  CONTINUE_GONE,
  DECISION_CONFLICT,
  DECISION_HELD,
  DECISION_UNCONFIRMED,
  DECISION_WAITING,
  accessFoot,
  answerOptions,
  askLevels,
  attemptKey,
  heldAttempt,
  validOffer,
  NOTE_GONE,
  authorName,
  cleanAccessStatus,
  cleanDecisions,
  cleanNote,
  cleanRequest,
  continueInit,
  countOf,
  cursorOf,
  continueReduce,
  decisionAnswered,
  decisionCard,
  decisionFailed,
  mayGive,
  noteActions,
  noteCountWords,
  noteCounter,
  noteFailure,
  noteFits,
  noteMarks,
  notesInit,
  notesReduce as reduceNotes,
  ownerRaceLine,
  peerChoiceLabel,
  peerNotice,
  pendingAccess,
  remoteMenu,
  seedParts,
  sendPeers,
  settledShown,
  startedFrom,
  stripState,
  threadOf,
  type ContinueState,
  type NotesEvent,
  type NotesState,
} from "./federationTogether";

type PageEvent = Extract<NotesEvent, { type: "page" }>;
/** The reducer, for the tests that read in order: a page that names no `after` was asked from
 *  the cursor the thread stands at (what one read at a time always does). The out-of-order
 *  tests name `after` themselves. */
const notesReduce = (s: NotesState, e: Exclude<NotesEvent, PageEvent> | (Omit<PageEvent, "after"> & { after?: number })) =>
  reduceNotes(s, e.type === "page" ? { ...e, after: e.after ?? threadOf(s, e.grantId).seq } : e);

const NOW = 1_800_000_000;
const G1 = "grant1".padEnd(22, "x");
const G2 = "grant2".padEnd(22, "x");
const grant = (over: Partial<FederationGrant> = {}): FederationGrant => ({
  grant_id: G1,
  session_key: "codex-api:abc",
  peer_id: "p".repeat(32),
  peer_name: "gpu-box",
  level: "view",
  expires_at: NOW + 5 * 86400,
  revision: 1,
  state: "live",
  created_at: NOW - 60,
  ended_at: null,
  end_reason: null,
  ...over,
});
const note = (id: string, over: Record<string, unknown> = {}) => ({
  note_id: id.padEnd(22, "x"),
  author: "peer",
  mine: false,
  text: "hello",
  created_at: NOW,
  edited_at: null,
  deleted: false,
  ...over,
});

// ---- presence ------------------------------------------------------------------------------------

describe("the owner's strip", () => {
  test("live grants only, a name, a level, an end and one bit", () => {
    const s = stripState(
      [
        grant({ watching: true, notes: 3 }),
        grant({ grant_id: G2, peer_name: "render-box", level: "send", state: "ended" }),
        grant({ grant_id: "old".padEnd(22, "x"), expires_at: NOW - 1 }),
      ],
      NOW,
    );
    expect(s.rows).toHaveLength(1);
    expect(s.rows[0]).toMatchObject({ peer: "gpu-box", access: "can view", watching: true, notes: 3 });
    expect(s.collapsed).toBe(false);
  });
  test("watching is true only when the server says exactly true", () => {
    const s = stripState([grant({ watching: "yes" as unknown as boolean })], NOW);
    expect(s.rows[0].watching).toBe(false);
  });
  test("two rows are listed; three fold into a count; the operator's own choice wins", () => {
    const two = [grant(), grant({ grant_id: G2, peer_name: "render-box", level: "send" })];
    const three = [...two, grant({ grant_id: "g3".padEnd(22, "x"), peer_name: "third" })];
    expect(stripState(two, NOW).collapsed).toBe(false);
    expect(stripState(three, NOW).collapsed).toBe(true);
    expect(stripState(three, NOW).summary).toBe("Shared with 3 instances");
    expect(stripState(three, NOW, true).collapsed).toBe(false);
    expect(stripState(two, NOW, false).collapsed).toBe(true);
    // One grant is always its row, whatever was chosen.
    expect(stripState([grant()], NOW, false).collapsed).toBe(false);
  });
  test("a garbage note count is never shown as a number of notes", () => {
    expect(stripState([grant({ notes: -4 })], NOW).rows[0].notes).toBe(0);
    expect(stripState([grant({ notes: Number.NaN })], NOW).rows[0].notes).toBe(0);
  });
  test("the owner's race line names who can send, and nobody below send", () => {
    const peers = sendPeers(
      [grant({ level: "send" }), grant({ grant_id: G2, peer_name: "render-box", level: "suggest" })],
      NOW,
    );
    expect(peers).toEqual(["gpu-box"]);
    expect(ownerRaceLine(peers)).toBe(
      "gpu-box can send, so it may also approve this once or reject it. The first answer counts.",
    );
    expect(ownerRaceLine([])).toBeNull();
  });
});

// ---- access requests -------------------------------------------------------------------------------

describe("asking for access", () => {
  test("only the levels above the current one", () => {
    expect(askLevels("view")).toEqual(["suggest", "send"]);
    expect(askLevels("suggest")).toEqual(["send"]);
    expect(askLevels("send")).toEqual([]);
    expect(askLevels("owner")).toEqual([]);
    expect(askLevels(null)).toEqual([]);
  });
  const status = (level: string, state: string, asked = "send") =>
    cleanAccessStatus({ level, request: { request_id: "r".repeat(22), level: asked, state } });
  test("pending is waiting; declined says so; granted, withdrawn and expired show nothing", () => {
    expect(accessFoot(status("view", "pending"), "view", "studio")).toEqual({
      kind: "waiting",
      text: "Request sent · waiting for studio's operator",
    });
    expect(accessFoot(status("view", "declined"), "view", "studio")).toEqual({
      kind: "declined",
      text: "studio's operator declined.",
    });
    for (const state of ["granted", "withdrawn", "expired"])
      expect(accessFoot(status("view", state), "view", "studio")).toEqual({ kind: "none" });
  });
  test("a request for a level the grant already has is over, whatever its state says", () => {
    expect(accessFoot(status("send", "pending"), "send", "studio")).toEqual({ kind: "none" });
    expect(accessFoot(status("suggest", "pending", "suggest"), "suggest", "studio")).toEqual({ kind: "none" });
  });
  test("an answer with a malformed request is no answer", () => {
    expect(cleanAccessStatus({ level: "view", request: { request_id: "x", state: "pending" } })).toBeNull();
    expect(cleanAccessStatus({})).toBeNull();
    expect(cleanAccessStatus({ level: "view", request: null })).toEqual({ level: "view", request: null });
  });
  test("the owner's cards: pending ones only, oldest first, the note as text", () => {
    const rows = pendingAccess([
      { request_id: "b".repeat(22), level: "send", state: "pending", note: "<b>x</b>" },
      { request_id: "c".repeat(22), level: "send", state: "declined" },
      { request_id: "a".repeat(22), level: "suggest", state: "pending" },
      { request_id: "d".repeat(22), level: "owner", state: "pending" },
    ]);
    expect(rows.map((r) => r.request_id[0])).toEqual(["a", "b"]);
    expect(rows[1].note).toBe("<b>x</b>");
  });
  test("bell rows: the kind from the server's action id, a grant id only of a valid shape", () => {
    expect(peerNotice({ action_id: "peer-access:abc" })).toEqual({ kind: "access", label: "Access request" });
    expect(peerNotice({ action_id: `peer-note:${G1}` })).toEqual({ kind: "note", label: "Note", grantId: G1 });
    expect(peerNotice({ action_id: "peer-note:../x" })).toBeNull();
    expect(peerNotice({ action_id: `peer-remote-note:${"a".repeat(32)}:${G1}` })).toMatchObject({
      kind: "remote",
      what: "note",
      grantId: G1,
    });
    expect(peerNotice({ action_id: "automation:x:1" })).toBeNull();
  });
});

// ---- decisions -------------------------------------------------------------------------------------

describe("a decision card on the receiving side", () => {
  const OFFER = "Zm9vYmFy-_Zm9vYmFy-_Zm9vYmFy-_01";
  const cmd = cleanRequest({
    request_id: "req-7",
    turn_id: "t1",
    kind: "command",
    summary: "pytest",
    choices: ["approve", "always", "reject"],
    offer: OFFER,
  })!;
  test("a send peer may answer once or reject; `always` is drawn and NEVER enabled", () => {
    const card = decisionCard(cmd, "send", "studio");
    expect(card.role).toBe("answer");
    expect(card.word).toBe("you may answer");
    expect(card.buttons.map((b) => [b.choice, b.enabled])).toEqual([
      ["approve", true],
      ["always", false],
      ["reject", true],
    ]);
    expect(card.line).toBe("Only studio's operator can allow this always.");
    expect(mayGive(card, "approve")).toBe(true);
    expect(mayGive(card, "always")).toBe(false);
    // Whatever a forged card says, `always` is refused by the gate.
    const forged = { ...card, buttons: card.buttons.map((b) => ({ ...b, enabled: true })) };
    expect(mayGive(forged, "always")).toBe(false);
  });
  test("view and suggest only watch; so does send when the owner lists nothing", () => {
    for (const level of ["view", "suggest", null])
      expect(decisionCard(cmd, level, "studio")).toMatchObject({ role: "watch", buttons: [], line: DECISION_WAITING });
    const none = cleanRequest({ request_id: "r", kind: "command", choices: [] })!;
    expect(decisionCard(none, "send", "studio").role).toBe("watch");
    expect(mayGive(decisionCard(cmd, "view", "studio"), "approve")).toBe(false);
  });
  test("a choice the owner did not list is drawn disabled", () => {
    const only = cleanRequest({ request_id: "r", kind: "command", choices: ["reject"], offer: OFFER })!;
    const card = decisionCard(only, "send", "studio");
    expect(card.buttons.find((b) => b.choice === "approve")?.enabled).toBe(false);
    expect(mayGive(card, "approve")).toBe(false);
  });
  test("choices are words of a fixed shape; anything else is dropped", () => {
    const r = cleanRequest({ request_id: "r", kind: "command", choices: ["approve", "<b>", 3, "Always"] })!;
    expect(r.choices).toEqual(["approve"]);
  });
  test("the labels are the app's own (`choiceLabel`)", () => {
    for (const kind of ["command", "file_change", "mcp_tool"])
      for (const choice of ["approve", "always", "reject"])
        expect(peerChoiceLabel(choice, kind, false)).toBe(
          choiceLabel(choice, { kind, choices: [] } as unknown as StructuredRequest),
        );
    const q = { kind: "AskUserQuestion", choices: [], questions: [{ question: "?", options: [] }] };
    for (const choice of ["answer", "reject"])
      expect(peerChoiceLabel(choice, "AskUserQuestion", true)).toBe(
        choiceLabel(choice, q as unknown as StructuredRequest),
      );
  });
  test("a question is answered by option INDEX, complete only when every question has one", () => {
    const qs = [
      { question: "a", multi: false, options: ["x", "y"] },
      { question: "b", multi: true, options: ["p", "q", "r"] },
    ];
    expect(answerOptions(qs, [[1], []])).toBeNull();
    expect(answerOptions(qs, [[1, 0], [2, 0, 2, 9]])).toEqual([[1], [0, 2]]);
  });
  test("an offer is 32 url-safe characters and nothing else; any other value is no offer", () => {
    expect(OFFER).toHaveLength(32);
    expect(cmd.offer).toBe(OFFER);
    const bad = [
      undefined,
      null,
      "",
      OFFER.slice(1), // 31
      `${OFFER}A`, // 33
      `${OFFER.slice(0, 31)}=`,
      `${OFFER.slice(0, 31)}/`,
      `${OFFER.slice(0, 31)} `,
      `${OFFER.slice(0, 31)}\n`,
      `${OFFER}\n`,
      `${OFFER.slice(0, 31)}é`,
      7,
      [OFFER],
      { toString: () => OFFER },
    ];
    for (const offer of bad) {
      expect(validOffer(offer)).toBe(false);
      const r = cleanRequest({ request_id: "r", turn_id: "t1", kind: "command", choices: ["approve", "reject"], offer })!;
      expect(r.offer).toBeNull();
    }
    expect(validOffer(OFFER)).toBe(true);
  });
  test("choices without a well-formed offer are not answerable: the card only watches (fail closed)", () => {
    for (const offer of [undefined, null, "", "short", `${OFFER}x`, 5]) {
      const r = cleanRequest({ request_id: "r", turn_id: "t1", kind: "command", choices: ["approve", "reject"], offer })!;
      expect(r.choices).toEqual(["approve", "reject"]);
      const card = decisionCard(r, "send", "studio");
      expect(card).toMatchObject({ role: "watch", buttons: [], line: DECISION_WAITING });
      for (const choice of ["approve", "reject", "always"]) expect(mayGive(card, choice)).toBe(false);
    }
    const q = cleanRequest({
      request_id: "q",
      turn_id: "t1",
      kind: "AskUserQuestion",
      choices: ["answer", "reject"],
      questions: [{ question: "?", multi: false, options: ["a"] }],
    })!;
    expect(decisionCard(q, "send", "studio")).toMatchObject({ role: "watch", buttons: [] });
  });
  test("one key per attempt at one answer to one offer; a retry of the same answer keeps both", () => {
    let n = 0;
    const mint = () => `k${++n}`;
    const first = attemptKey(null, "t\0r", "approve", OFFER, mint);
    expect(first).toEqual({ requestKey: "t\0r", answer: "approve", offer: OFFER, client_key: "k1" });
    expect(attemptKey(first, "t\0r", "approve", OFFER, mint)).toBe(first);
    expect(attemptKey(first, "t\0r", "reject", OFFER, mint).client_key).toBe("k2");
    expect(attemptKey(first, "t\0other", "approve", OFFER, mint).client_key).toBe("k3");
    // The same request id under ANOTHER offer is another request: a new key, and the new offer.
    const other = "B".repeat(32);
    expect(attemptKey(first, "t\0r", "approve", other, mint)).toEqual({
      requestKey: "t\0r",
      answer: "approve",
      offer: other,
      client_key: "k4",
    });
  });
  test("after not_pending no attempt is held: the next answer gets a new key", () => {
    const attempt = { requestKey: "t\0r", answer: "approve", offer: OFFER, client_key: "k1" };
    expect(heldAttempt(null)).toBeNull();
    expect(heldAttempt({ attempt, outcome: null })).toBe(attempt);
    expect(heldAttempt({ attempt, outcome: { kind: "unconfirmed", text: "" } })).toBe(attempt);
    expect(heldAttempt({ attempt, outcome: decisionFailed(409, "held", "studio") })).toBe(attempt);
    expect(heldAttempt({ attempt, outcome: decisionFailed(409, "not_pending", "studio") })).toBeNull();
    let n = 1;
    const next = attemptKey(heldAttempt({ attempt, outcome: decisionFailed(409, "not_pending", "studio") }), "t\0r", "approve", OFFER, () => `k${++n}`);
    expect(next.client_key).toBe("k2");
  });
  test("each server code reads as its own state", () => {
    expect(decisionAnswered({ outcome: "accepted" }, "studio")).toEqual({ kind: "accepted" });
    expect(decisionAnswered({ outcome: "settled", author: { via: "owner" } }, "studio")).toEqual({
      kind: "lost",
      text: "Already answered by studio's operator.",
    });
    expect(decisionAnswered({ outcome: "settled", author: { via: "peer", name: "lab" } }, "studio")).toEqual({
      kind: "lost",
      text: "Already answered by lab.",
    });
    expect(decisionAnswered({}, "studio")).toEqual({ kind: "unconfirmed", text: DECISION_UNCONFIRMED });
    expect(decisionFailed(404, "no_grant", "studio")).toEqual({ kind: "no_grant" });
    expect(decisionFailed(503, "peer_offline", "studio")).toEqual({ kind: "offline" });
    expect(decisionFailed(403, "owner_only", "studio")).toMatchObject({
      code: "owner_only",
      text: "Only studio's operator can allow this.",
    });
    expect(decisionFailed(409, "held", "studio")).toMatchObject({ code: "held", text: DECISION_HELD });
    expect(decisionFailed(409, "conflict", "studio")).toMatchObject({ code: "conflict", text: DECISION_CONFLICT });
    expect(decisionFailed(409, "not_pending", "studio")).toMatchObject({ code: "not_pending" });
    for (const status of [0, 408, 500, 502, 504])
      expect(decisionFailed(status, undefined, "studio").kind).toBe("unconfirmed");
  });
  test("what proves nothing is never a refusal: peer_lost, a timeout, busy, anything unlisted (#2061)", () => {
    const unknown: [number, unknown][] = [
      [504, "peer_lost"],
      [504, "timeout"],
      [429, "busy"], // an owner on an older build answered `busy` AFTER the effect
      [503, "unavailable"],
      [502, "bad_answer"],
      [404, "something_new"],
      [409, "something_new"],
      [418, "teapot"],
      [409, undefined],
    ];
    for (const [status, code] of unknown)
      expect(decisionFailed(status, code, "studio"), `${status} ${String(code)}`).toEqual({
        kind: "unconfirmed",
        text: DECISION_UNCONFIRMED,
      });
    // It agrees with the shared classifier on every one of them: no second table.
    for (const [status, code] of unknown) expect(writeFailure(status, code)).toBe("unconfirmed");
  });
  test("an unconfirmed attempt locks the card to its own answer; a settled or refused one does not", () => {
    const attempt = attemptKey(null, "t\0r", answerSignature("answer", [[1], [0, 2]]), OFFER, () => "k1");
    expect(attemptAnswer(attempt)).toEqual({ choice: "answer", options: [[1], [0, 2]] });
    expect(lockedAnswer({ attempt, outcome: decisionFailed(504, "peer_lost", "studio") })).toEqual({
      choice: "answer",
      options: [[1], [0, 2]],
    });
    expect(lockedAnswer({ attempt, outcome: null })).toBeNull();
    expect(lockedAnswer({ attempt, outcome: { kind: "accepted" } })).toBeNull();
    expect(lockedAnswer({ attempt, outcome: decisionFailed(409, "held", "studio") })).toBeNull();
    expect(lockedAnswer(null)).toBeNull();
    expect(attemptAnswer({ answer: "not json" })).toBeNull();
    // The retry of the locked answer is the same attempt: its key and its offer.
    const again = attemptKey(heldAttempt({ attempt, outcome: decisionFailed(504, "peer_lost", "studio") }), "t\0r", answerSignature("answer", [[1], [0, 2]]), OFFER, () => "k2");
    expect(again).toBe(attempt);
  });
  test("an access request or withdrawal: only a proven refusal says nothing changed", () => {
    expect(accessFailure(404, "no_grant")).toBe("ended");
    expect(accessFailure(503, "peer_offline")).toBe("offline");
    expect(accessFailure(422, "invalid")).toBe("rejected");
    expect(accessFailure(422, "not_possible")).toBe("rejected");
    expect(accessFailure(409, "conflict")).toBe("rejected");
    expect(accessFailure(403, "forbidden")).toBe("rejected");
    for (const [status, code] of [[504, "peer_lost"], [504, "timeout"], [429, "busy"], [0, undefined], [500, "x"], [418, "teapot"], [503, "unavailable"]] as const)
      expect(accessFailure(status, code), `${status} ${String(code)}`).toBe("unconfirmed");
  });
  test("settled: the stored author, by the name each side shows", () => {
    const [d] = cleanDecisions([
      { request_id: "req-7", turn_id: "t1", answer: "once", author: { via: "peer", name: "gpu-box" }, mine: false },
      { request_id: "x", answer: "<b>", author: { via: "peer" } },
      { request_id: "y", answer: "once", author: { via: "agent" } },
    ]);
    expect(authorName(d, "owner", "")).toBe("gpu-box");
    expect(authorName({ ...d, mine: true }, "peer", "studio")).toBe("you");
    expect(authorName({ ...d, author: { via: "owner", name: "" } }, "peer", "studio")).toBe("studio's operator");
    expect(authorName({ ...d, author: { via: "owner", name: "" } }, "owner", "")).toBe("you");
    expect(cleanDecisions([{ request_id: "x", answer: "<b>", author: { via: "peer" } }])).toEqual([]);
  });
  test("a still-pending request is never shown settled; an unseen old one is not shown", () => {
    const ds = cleanDecisions([
      { request_id: "a", turn_id: "t0", answer: "once", author: { via: "owner" } },
      { request_id: "b", turn_id: "t1", answer: "once", author: { via: "owner" } },
      { request_id: "c", turn_id: "t1", answer: "decline", author: { via: "owner" } },
    ]);
    const pending = new Set([ds[2].key]);
    expect(settledShown(ds, pending, new Map(), "t1").map((d) => d.request_id)).toEqual(["b"]);
    const seen = new Map([[ds[0].key, { kind: "command", summary: "", question: null }]]);
    expect(settledShown(ds, new Set(), seen, "t9").map((d) => d.request_id)).toEqual(["a"]);
  });
});

// ---- notes -----------------------------------------------------------------------------------------

describe("the notes reducer", () => {
  const page = (changes: unknown[], seq: number, reset = false) => ({ reset, seq, changes, more: false });
  const open = (grantId: string, s: NotesState = notesInit) => notesReduce(s, { type: "page", grantId, page: page([], 0) });

  test("changes by cursor: add, edit, hide (removed), tombstone, and eviction", () => {
    let s = open(G1);
    s = notesReduce(s, {
      type: "page",
      grantId: G1,
      page: page([{ seq: 1, note_id: note("a").note_id, note: note("a") }, { seq: 2, note_id: note("b").note_id, note: note("b", { created_at: NOW + 1 }) }], 2),
    });
    expect(threadOf(s, G1).notes.map((n) => n.note_id[0])).toEqual(["a", "b"]);
    expect(threadOf(s, G1).seq).toBe(2);
    // an edit of an old note arrives through the cursor
    s = notesReduce(s, {
      type: "page",
      grantId: G1,
      page: page([{ seq: 3, note_id: note("a").note_id, note: note("a", { text: "edited", edited_at: NOW + 5 }) }], 3),
    });
    expect(threadOf(s, G1).notes[0]).toMatchObject({ text: "edited", edited_at: NOW + 5 });
    expect(noteMarks(threadOf(s, G1).notes[0], "gpu-box").marks).toEqual(["edited"]);
    // a delete is a tombstone with no text
    s = notesReduce(s, {
      type: "page",
      grantId: G1,
      page: page([{ seq: 4, note_id: note("b").note_id, note: note("b", { text: "still here?", deleted: true }) }], 4),
    });
    const tomb = threadOf(s, G1).notes[1];
    expect(tomb).toMatchObject({ deleted: true, text: "" });
    expect(noteMarks(tomb, "gpu-box").marks).toEqual(["deleted"]);
    expect(noteActions({ ...tomb, mine: true }, "peer", true)).toEqual([]);
    // a hide (receiver) or an eviction is `removed`: the note leaves the thread
    s = notesReduce(s, { type: "page", grantId: G1, page: page([{ seq: 5, note_id: note("a").note_id, removed: true }], 5) });
    expect(threadOf(s, G1).notes.map((n) => n.note_id[0])).toEqual(["b"]);
    // the cursor never goes back
    s = notesReduce(s, { type: "page", grantId: G1, page: page([], 1) });
    expect(threadOf(s, G1).seq).toBe(5);
  });
  test("an older answer never overwrites a newer one: the removed note stays removed and the cursor stays", () => {
    // Two reads were asked from the same cursor. A was cut first (the note exists, seq 1) and is
    // delayed; B was cut after the owner hid the note (its removal, seq 2) and lands first.
    const A = page([{ seq: 1, note_id: note("a").note_id, note: note("a", { text: "HIDDEN" }) }], 1);
    const B = page([{ seq: 2, note_id: note("a").note_id, removed: true }], 2);
    let s = open(G1);
    s = reduceNotes(s, { type: "page", grantId: G1, page: B, after: 0 });
    expect(threadOf(s, G1)).toMatchObject({ notes: [], seq: 2 });
    const late = reduceNotes(s, { type: "page", grantId: G1, page: A, after: 0 });
    expect(late).toBe(s); // dropped whole: no note, and the cursor is still 2
    expect(threadOf(late, G1)).toMatchObject({ notes: [], seq: 2 });
    // The other order is safe too: A lands, B (asked from 0 as well) is dropped, and the next
    // read, asked from 1, delivers the removal.
    let t = reduceNotes(open(G1), { type: "page", grantId: G1, page: A, after: 0 });
    t = reduceNotes(t, { type: "page", grantId: G1, page: B, after: 0 });
    expect(threadOf(t, G1)).toMatchObject({ seq: 1 });
    expect(threadOf(t, G1).notes).toHaveLength(1);
    t = reduceNotes(t, { type: "page", grantId: G1, page: B, after: 1 });
    expect(threadOf(t, G1)).toMatchObject({ notes: [], seq: 2 });
    // A stale RESET page is dropped as well: it would replace the thread with an older one.
    const reset = page([{ seq: 1, note_id: note("a").note_id, note: note("a", { text: "HIDDEN" }) }], 1, true);
    expect(reduceNotes(s, { type: "page", grantId: G1, page: reset, after: 0 })).toBe(s);
    // After the thread cleared (offline: cursor 0 again) a page asked from the old cursor is stale.
    const cleared = reduceNotes(s, { type: "failed", grantId: G1, status: 503 });
    expect(reduceNotes(cleared, { type: "page", grantId: G1, page: page([], 3), after: 2 })).toBe(cleared);
  });
  describe("a write's late answer never moves the thread back", () => {
    const mine = (over: Record<string, unknown> = {}) => note("m", { author: "peer", mine: true, text: "hi", created_at: NOW, ...over });
    const added = { seq: 1, note_id: mine().note_id, note: mine() };
    const removed = { seq: 2, note_id: mine().note_id, removed: true };
    const sent = () => {
      let s = notesReduce(open(G1), { type: "draft", grantId: G1, text: "hi" });
      s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
      return s;
    };
    test("the add's answer after a read consumed the add AND its removal inserts nothing", () => {
      let s = sent();
      s = notesReduce(s, { type: "page", grantId: G1, page: page([added], 1) }); // the read sees the note
      expect(threadOf(s, G1)).toMatchObject({ pending: [], draft: "", seq: 1 });
      expect(threadOf(s, G1).notes).toHaveLength(1);
      s = notesReduce(s, { type: "page", grantId: G1, page: page([removed], 2) }); // the owner hid it
      expect(threadOf(s, G1)).toMatchObject({ notes: [], seq: 2 });
      // The old answer arrives: the hidden text does not come back, and the cursor stays.
      const late = notesReduce(s, { type: "confirmed", grantId: G1, client_key: "k1", note: mine(), seq: 1 });
      expect(threadOf(late, G1)).toMatchObject({ notes: [], pending: [], seq: 2 });
      // …and an empty read after 2 leaves it so.
      const after = notesReduce(late, { type: "page", grantId: G1, page: page([], 2) });
      expect(threadOf(after, G1)).toMatchObject({ notes: [], seq: 2 });
    });
    test("the add's answer AT the cursor (the read brought a newer edit of it) does not put the old text back", () => {
      let s = sent();
      s = notesReduce(s, { type: "page", grantId: G1, page: page([added], 1) });
      s = notesReduce(s, { type: "page", grantId: G1, page: page([{ seq: 2, note_id: mine().note_id, note: mine({ text: "edited", edited_at: NOW + 1 }) }], 2) });
      const late = notesReduce(s, { type: "confirmed", grantId: G1, client_key: "k1", note: mine(), seq: 1 });
      expect(threadOf(late, G1).notes.map((n) => n.text)).toEqual(["edited"]);
    });
    test("the add's answer AHEAD of the cursor inserts once; the next read from the same cursor reconciles by id", () => {
      let s = notesReduce(sent(), { type: "confirmed", grantId: G1, client_key: "k1", note: mine(), seq: 1 });
      expect(threadOf(s, G1)).toMatchObject({ pending: [], draft: "", held: null, seq: 0 }); // the cursor did not move
      expect(threadOf(s, G1).notes).toHaveLength(1);
      // the read at the same change: exactly one row, no duplicate
      const same = notesReduce(s, { type: "page", grantId: G1, page: page([added], 1) });
      expect(threadOf(same, G1).notes).toHaveLength(1);
      expect(threadOf(same, G1).seq).toBe(1);
      // or the owner hid it meanwhile: the read from the OLD cursor carries the removal
      s = notesReduce(s, { type: "page", grantId: G1, page: page([removed], 2) });
      expect(threadOf(s, G1)).toMatchObject({ notes: [], seq: 2 });
    });
    test("an add's answer with no usable change number inserts nothing: the row waits, saved, for the next read", () => {
      for (const seq of [undefined, Number.NaN, -1, "1", 1e300]) {
        let s = notesReduce(sent(), { type: "confirmed", grantId: G1, client_key: "k1", note: mine(), seq });
        expect(threadOf(s, G1).notes, String(seq)).toHaveLength(0);
        expect(threadOf(s, G1)).toMatchObject({ draft: "", held: null, pending: [{ client_key: "k1", state: "saved" }] });
        // the read brings the note (or does not): either way the row goes with it
        s = notesReduce(s, { type: "thread", grantId: G1, notes: [mine()], ended: false, seq: 1 });
        expect(threadOf(s, G1)).toMatchObject({ pending: [] });
        expect(threadOf(s, G1).notes).toHaveLength(1);
      }
    });
    test("an answer for a thread that ended, went offline or is read-only settles and inserts nothing", () => {
      for (const status of [404, 503]) {
        const off = notesReduce(sent(), { type: "failed", grantId: G1, status });
        const late = notesReduce(off, { type: "confirmed", grantId: G1, client_key: "k1", note: mine(), seq: 9 });
        expect(threadOf(late, G1)).toMatchObject({ notes: [], pending: [] });
      }
    });
  });
  test("an access status that says a request is pending after it was over is the older answer", () => {
    const id = "r".repeat(22);
    const pending = cleanAccessStatus({ level: "view", request: { request_id: id, level: "send", state: "pending" } });
    const other = cleanAccessStatus({ level: "view", request: { request_id: "s".repeat(22), level: "send", state: "pending" } });
    expect(accessStatusIsOlder(new Set(), pending)).toBe(false);
    expect(accessStatusIsOlder(new Set([id]), pending)).toBe(true);
    expect(accessStatusIsOlder(new Set([id]), other)).toBe(false); // a NEW request after it
    expect(accessStatusIsOlder(new Set([id]), cleanAccessStatus({ level: "view", request: null }))).toBe(false);
    expect(accessStatusIsOlder(new Set([id]), null)).toBe(false);
  });
  test("the owner's whole thread: one cut from an older state is dropped", () => {
    let s = reduceNotes(notesInit, { type: "thread", grantId: G1, notes: [], ended: false, seq: 2 });
    const late = reduceNotes(s, { type: "thread", grantId: G1, notes: [note("a", { text: "DELETED" })], ended: false, seq: 1 });
    expect(late).toBe(s);
    s = reduceNotes(s, { type: "thread", grantId: G1, notes: [note("b")], ended: false, seq: 3 });
    expect(threadOf(s, G1)).toMatchObject({ seq: 3 });
    expect(threadOf(s, G1).notes).toHaveLength(1);
    // a thread that names no change number (an ended one) is taken as it is
    s = reduceNotes(s, { type: "thread", grantId: G1, notes: [], ended: true });
    expect(threadOf(s, G1)).toMatchObject({ status: "ended", notes: [], seq: 3 });
  });
  test("a reset replaces the thread", () => {
    let s = notesReduce(open(G1), { type: "page", grantId: G1, page: page([{ seq: 1, note_id: note("a").note_id, note: note("a") }], 7) });
    s = notesReduce(s, { type: "page", grantId: G1, page: page([{ seq: 2, note_id: note("z").note_id, note: note("z") }], 2, true) });
    expect(threadOf(s, G1).notes.map((n) => n.note_id[0])).toEqual(["z"]);
    expect(threadOf(s, G1).seq).toBe(2);
  });
  test("the owner's hide is a mark, and only the owner may hide a peer's note", () => {
    const hidden = cleanNote(note("a", { hidden: true }))!;
    expect(noteMarks(hidden, "gpu-box").marks).toEqual(["hidden from gpu-box"]);
    const peers = cleanNote(note("b"))!;
    expect(noteActions(peers, "owner", true)).toEqual(["hide"]);
    expect(noteActions(peers, "peer", true)).toEqual([]);
    expect(noteActions(hidden, "owner", true)).toEqual([]);
    expect(noteActions(cleanNote(note("c", { mine: true }))!, "owner", true)).toEqual(["edit", "delete"]);
    expect(noteActions(cleanNote(note("c", { mine: true }))!, "owner", false)).toEqual([]);
  });
  test("a note's text is kept as characters; a malformed id is dropped", () => {
    expect(cleanNote(note("a", { text: "<img src=x onerror=1>" }))?.text).toBe("<img src=x onerror=1>");
    expect(cleanNote({ ...note("a"), note_id: "../../x" })).toBeNull();
  });
  test("keyed by grant: a draft, a pending write and a cursor never cross threads", () => {
    let s = open(G2, open(G1));
    s = notesReduce(s, { type: "draft", grantId: G1, text: "for gpu-box" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "for gpu-box", now: NOW });
    s = notesReduce(s, { type: "page", grantId: G1, page: page([], 9) });
    expect(threadOf(s, G2)).toMatchObject({ draft: "", pending: [], held: null, seq: 0 });
    // an answer for G1 lands in G1 only
    s = notesReduce(s, { type: "unconfirmed", grantId: G1, client_key: "k1" });
    expect(threadOf(s, G1).pending).toMatchObject([{ client_key: "k1", state: "unconfirmed" }]);
    expect(threadOf(s, G2).pending).toEqual([]);
    // the pane cleared: nothing of any thread stays
    expect(notesReduce(s, { type: "reset" })).toEqual(notesInit);
  });
  test("write states: sending → confirmed clears the draft; not confirmed keeps it and its key", () => {
    let s = open(G1);
    s = notesReduce(s, { type: "draft", grantId: G1, text: "hi" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    expect(threadOf(s, G1)).toMatchObject({ draft: "hi", held: { text: "hi", key: "k1" } });
    expect(threadOf(s, G1).pending[0].state).toBe("sending");
    const lost = notesReduce(s, { type: "unconfirmed", grantId: G1, client_key: "k1" });
    expect(threadOf(lost, G1)).toMatchObject({ draft: "hi", held: { key: "k1" } });
    expect(threadOf(lost, G1).pending[0].state).toBe("unconfirmed");
    // Retry under the same key: still ONE pending row, and the confirmed note is the only one.
    const again = notesReduce(lost, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    expect(threadOf(again, G1).pending).toHaveLength(1);
    const done = notesReduce(again, { type: "confirmed", grantId: G1, client_key: "k1", note: note("n", { mine: true, text: "hi" }), seq: 1 });
    expect(threadOf(done, G1)).toMatchObject({ draft: "", held: null, pending: [] });
    expect(threadOf(done, G1).notes).toHaveLength(1);
  });
  test("a note that arrives by reading settles the unconfirmed write it became", () => {
    let s = open(G1);
    s = notesReduce(s, { type: "draft", grantId: G1, text: "hi" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    s = notesReduce(s, { type: "unconfirmed", grantId: G1, client_key: "k1" });
    s = notesReduce(s, { type: "page", grantId: G1, page: page([{ seq: 1, note_id: note("n").note_id, note: note("n", { mine: true, text: "hi" }) }], 1) });
    expect(threadOf(s, G1)).toMatchObject({ pending: [], draft: "", held: null });
  });
  test("a refusal is a confirmed failure: the row goes, the draft and its key stay", () => {
    let s = open(G1);
    s = notesReduce(s, { type: "draft", grantId: G1, text: "hi" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    s = notesReduce(s, { type: "refused", grantId: G1, client_key: "k1", text: "rate" });
    expect(threadOf(s, G1)).toMatchObject({ pending: [], draft: "hi", refusal: "rate", held: { key: "k1" } });
    expect(noteFailure(429, "rate")).toBe("rate");
    expect(noteFailure(404, "no_grant")).toBe("ended");
    expect(noteFailure(404, "no_grant", "peer")).toBe("ended");
    // `not_found` on the receiving side is the NOTE (the owner hid it), never the share.
    expect(noteFailure(404, "not_found", "peer")).toBe("missing");
    expect(noteFailure(404, "not_found", "owner")).toBe("ended");
    expect(noteFailure(409, "ended")).toBe("ended");
    expect(noteFailure(503, "peer_offline")).toBe("offline");
    expect(noteFailure(422, "invalid")).toBe("rejected");
    for (const status of [0, 408, 500, 504]) expect(noteFailure(status, undefined)).toBe("unconfirmed");
    // The receiving side's write crosses the channel: what proves nothing is not confirmed (#2061).
    expect(noteFailure(503, "peer_offline", "peer")).toBe("offline");
    expect(noteFailure(422, "invalid", "peer")).toBe("rejected");
    expect(noteFailure(413, "too_large", "peer")).toBe("rejected");
    for (const [status, code] of [[504, "peer_lost"], [504, "timeout"], [429, "busy"], [0, undefined], [500, "x"], [418, "teapot"], [404, "something_new"], [503, "unavailable"]] as const) {
      expect(noteFailure(status, code, "peer"), `${status} ${String(code)}`).toBe("unconfirmed");
      expect(writeFailure(status, code)).toBe("unconfirmed");
    }
    // The owner's own route is this instance's store: its 429 IS the hourly bound.
    expect(noteFailure(429, "busy", "owner")).toBe("rate");
  });
  test("a repeated add of a note the owner hid: the row, its key and its text go, the thread stays", () => {
    let s = notesReduce(open(G1), { type: "page", grantId: G1, page: page([{ seq: 1, note_id: note("a").note_id, note: note("a") }], 1) });
    s = notesReduce(s, { type: "draft", grantId: G1, text: "hi" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    s = notesReduce(s, { type: "unconfirmed", grantId: G1, client_key: "k1" });
    s = notesReduce(s, { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW }); // Retry
    s = notesReduce(s, { type: "missing", grantId: G1, client_key: "k1" });
    const t = threadOf(s, G1);
    expect(t).toMatchObject({ status: "live", pending: [], draft: "", held: null, refusal: NOTE_GONE });
    expect(t.notes.map((n) => n.note_id)).toEqual([note("a").note_id]); // the thread is not "gone"
    // Text typed since is the operator's next note, not the hidden one: it stays.
    let u = notesReduce(open(G1), { type: "submit", grantId: G1, client_key: "k1", text: "hi", now: NOW });
    u = notesReduce(u, { type: "draft", grantId: G1, text: "something else" });
    u = notesReduce(u, { type: "missing", grantId: G1, client_key: "k1" });
    expect(threadOf(u, G1)).toMatchObject({ pending: [], draft: "something else" });
  });
  test("an edit or delete of a note that is gone takes it out of the thread", () => {
    let s = notesReduce(open(G1), {
      type: "page",
      grantId: G1,
      page: page(
        [
          { seq: 1, note_id: note("a").note_id, note: note("a") },
          { seq: 2, note_id: note("b").note_id, note: note("b", { mine: true }) },
        ],
        2,
      ),
    });
    s = notesReduce(s, { type: "missing", grantId: G1, noteId: note("b").note_id });
    expect(threadOf(s, G1).notes.map((n) => n.note_id)).toEqual([note("a").note_id]);
    expect(threadOf(s, G1)).toMatchObject({ status: "live", refusal: NOTE_GONE });
  });
  test("offline and gone show no note; the draft stays until the drawer closes", () => {
    let s = notesReduce(open(G1), { type: "page", grantId: G1, page: page([{ seq: 1, note_id: note("a").note_id, note: note("a") }], 1) });
    s = notesReduce(s, { type: "draft", grantId: G1, text: "typed" });
    const off = notesReduce(s, { type: "failed", grantId: G1, status: 503 });
    expect(threadOf(off, G1)).toMatchObject({ status: "offline", notes: [], draft: "typed" });
    const gone = notesReduce(s, { type: "failed", grantId: G1, status: 404 });
    expect(threadOf(gone, G1)).toMatchObject({ status: "gone", notes: [], draft: "typed" });
    expect(threadOf(notesReduce(gone, { type: "close", grantId: G1 }), G1).draft).toBe("");
    // a live thread keeps its draft across a close
    expect(threadOf(notesReduce(s, { type: "close", grantId: G1 }), G1).draft).toBe("typed");
  });
  test("the owner's ended thread is readable and read-only", () => {
    const s = notesReduce(notesInit, { type: "thread", grantId: G1, notes: [note("a")], ended: true });
    expect(threadOf(s, G1)).toMatchObject({ status: "ended" });
    expect(threadOf(s, G1).notes).toHaveLength(1);
  });
  test("the counter counts characters, and 2000 is the bound", () => {
    expect(noteCounter("héllo")).toBe("5 / 2000");
    expect(noteFits("x".repeat(2000))).toBe(true);
    expect(noteFits("x".repeat(2001))).toBe(false);
    expect(noteFits("   ")).toBe(false);
  });
});

// ---- Continue here ---------------------------------------------------------------------------------

describe("the Continue-here stage machine", () => {
  const answer = { handle: "h1", preview: "seed", meta: { mode: "quick" } };
  const toPreview = (): ContinueState => {
    let s = continueReduce(continueInit, { type: "prepare", mode: "quick" });
    s = continueReduce(s, { type: "prepared", token: s.token, answer, mode: "quick" });
    return s;
  };
  test("prepare → preview; only `create` from the preview starts creating", () => {
    const s = toPreview();
    expect(s.view.stage).toBe("preview");
    // `create` is refused from every other stage
    expect(continueReduce(continueInit, { type: "create" }).view.stage).toBe("choose");
    const preparing = continueReduce(continueInit, { type: "prepare", mode: "ai" });
    expect(continueReduce(preparing, { type: "create" }).view.stage).toBe("preparing");
    expect(continueReduce(s, { type: "create" }).view.stage).toBe("creating");
  });
  test("Cancel and Back return to the start; a late answer after them changes nothing", () => {
    const preparing = continueReduce(continueInit, { type: "prepare", mode: "ai" });
    const back = continueReduce(preparing, { type: "back" });
    expect(back.view.stage).toBe("choose");
    const late = continueReduce(back, { type: "prepared", token: preparing.token, answer, mode: "ai" });
    expect(late.view.stage).toBe("choose");
    expect(continueReduce(toPreview(), { type: "back" }).view.stage).toBe("choose");
  });
  test("ended, offline and expired; a failed create spent the handle", () => {
    const preparing = continueReduce(continueInit, { type: "prepare", mode: "quick" });
    const fail = (s: ContinueState, status: number, code: string) =>
      continueReduce(s, { type: "failed", token: s.token, status, code, message: "nope", owner: "studio" });
    expect(fail(preparing, 404, "no_grant").view).toEqual({ stage: "ended", text: CONTINUE_GONE });
    expect(fail(preparing, 503, "peer_offline").view).toEqual({
      stage: "offline",
      text: "studio is not connected. Nothing was created.",
    });
    const creating = continueReduce(toPreview(), { type: "create" });
    expect(fail(creating, 409, "gone").view).toEqual({ stage: "ended", text: CONTINUE_GONE });
    expect(fail(creating, 410, "expired").view).toEqual({ stage: "choose", error: CONTINUE_EXPIRED });
    // a proven refusal at create: back to the start, never to a preview with a spent handle
    expect(fail(creating, 422, "invalid").view).toEqual({
      stage: "choose",
      error: "nope Nothing was created.",
    });
    expect(fail(creating, 404, "no_grant").view).toEqual({ stage: "ended", text: CONTINUE_GONE });
    // A create that proves nothing may have made the session: final, and never "Nothing was
    // created". No event leads from it to another create.
    for (const [status, code] of [[504, "peer_lost"], [0, ""], [500, "unavailable"], [502, "bad_answer"], [418, "teapot"], [409, "something_new"]] as const) {
      const lost = fail(creating, status, code);
      expect(lost.view, `${status} ${code}`).toEqual({ stage: "unconfirmed", text: CONTINUE_UNCONFIRMED });
      expect(continueCreateFailure(status, code)).toBe("unconfirmed");
      for (const event of [{ type: "create" }, { type: "prepare", mode: "quick" }, { type: "back" }] as const)
        expect(continueReduce(lost, event)).toBe(lost);
    }
    expect(continueCreateFailure(409, "gone")).toBe("gone");
    expect(continueCreateFailure(410, "expired")).toBe("expired");
    // the same failures while PREPARING created nothing, and say so by returning to the start
    expect(fail(preparing, 504, "timeout").view.stage).toBe("choose");
    // a stale token is ignored
    expect(continueReduce(creating, { type: "failed", token: 0, status: 404, code: "", message: "", owner: "s" })).toBe(
      creating,
    );
  });
  test("an answer without a handle prepares nothing", () => {
    let s = continueReduce(continueInit, { type: "prepare", mode: "quick" });
    s = continueReduce(s, { type: "prepared", token: s.token, answer: { preview: "x" }, mode: "quick" });
    expect(s.view.stage).toBe("choose");
  });
  test("the seed's record is found by its delimiters; without them all of it is the record", () => {
    const seed = ["lead", "", "<<<RECORD t0k", "| a", "RECORD t0k>>>", "", "tail"].join("\n");
    expect(seedParts(seed)).toEqual({ before: "lead", record: "<<<RECORD t0k\n| a\nRECORD t0k>>>", after: "tail" });
    expect(seedParts("no delimiters")).toEqual({ before: "", record: "no delimiters", after: "" });
    expect(seedParts("<<<RECORD t\nnever closed").record).toBe("<<<RECORD t\nnever closed");
  });
  test("the started-from memory is bounded tab memory", () => {
    startedFrom.clear();
    for (let i = 0; i < 20; i++) startedFrom.set(`k${i}`, `o${i}`);
    expect(startedFrom.get("k0")).toBeNull();
    expect(startedFrom.get("k19")).toBe("o19");
    startedFrom.clear();
  });
});

// ---- the remote pane's menu ------------------------------------------------------------------------

describe("the remote pane's menu", () => {
  test("each item only when it applies, and no session action", () => {
    expect(remoteMenu({ live: true, level: "view", pendingAsk: false })).toEqual(["continue", "notes", "ask"]);
    expect(remoteMenu({ live: true, level: "view", pendingAsk: true })).toEqual(["continue", "notes"]);
    expect(remoteMenu({ live: true, level: "send", pendingAsk: false })).toEqual(["continue", "notes"]);
    expect(remoteMenu({ live: false, level: "view", pendingAsk: false })).toEqual([]);
    expect(remoteMenu({ live: true, level: null, pendingAsk: false })).toEqual([]);
  });
});

// ---- what another instance says, as numbers (#2062 review) ---------------------------------------
// A peer's numbers are data, never trusted: a time, a count or a cursor that is not one never
// throws, never hides a row it should show, and never becomes a loop bound or a request the
// server refuses for ever. JSON cannot carry NaN or Infinity; a hostile build can still send
// 1e300, a negative or a string, and a test double sends the rest.
const HOSTILE: unknown[] = [1e300, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, -1, -1e300, "1800000000", null, {}, []];

describe("a peer's numbers, through the safe helpers only", () => {
  test("a cursor that is not a whole number the server can be asked after keeps the old one", () => {
    expect(cursorOf(7, 3)).toBe(7);
    expect(cursorOf(0, 3)).toBe(0);
    expect(cursorOf(2 ** 53 - 1, 3)).toBe(2 ** 53 - 1);
    for (const v of [...HOSTILE, 2 ** 53, 2.5]) expect(cursorOf(v, 3)).toBe(3);
  });
  test("a count is a whole number or 0, and its words stop at 999+", () => {
    expect(countOf(4)).toBe(4);
    for (const v of HOSTILE) expect(countOf(v)).toBe(0);
    expect(noteCountWords(0)).toBe("no notes");
    expect(noteCountWords(1)).toBe("1 note");
    expect(noteCountWords(12)).toBe("12 notes");
    expect(noteCountWords(1000)).toBe("999+ notes");
    for (const v of HOSTILE) expect(noteCountWords(v as number)).toBe("no notes");
  });
  test("the strip: a hostile count is no notes, a hostile expiry an unknown date, nothing throws", () => {
    for (const v of HOSTILE) {
      const s = stripState([grant({ notes: v as number, expires_at: v as number })], NOW);
      expect(s.rows).toHaveLength(1); // the server says it is live; this clock cannot say otherwise
      expect(s.rows[0].notes).toBe(0);
      expect(s.rows[0].until).toEqual({ text: "until an unknown date", countdown: false });
    }
    // an expiry that IS a date in the past is still ended here
    expect(stripState([grant({ expires_at: NOW - 1 })], NOW).rows).toHaveLength(0);
    expect(() => sendPeers([grant({ level: "send", expires_at: 1e300 as number })], NOW)).not.toThrow();
  });
  test("a note's times: a date is kept, anything else is 0 (an unknown time), and edited stays edited", () => {
    for (const v of HOSTILE) {
      const n = cleanNote(note("a", { created_at: v, edited_at: v }))!;
      expect(n.created_at).toBe(0);
      expect(n.edited_at).toBe(v === null ? null : 0);
    }
    expect(cleanNote(note("a", { edited_at: NOW + 3 }))!.edited_at).toBe(NOW + 3);
    expect(cleanNote(note("a", { edited_at: undefined }))!.edited_at).toBeNull();
  });
  test("the notes cursor: a hostile seq never moves the thread's cursor and never resets it", () => {
    const at = (seq: unknown, from: NotesState = notesInit) =>
      notesReduce(from, { type: "page", grantId: G1, page: { reset: false, seq, changes: [], more: false } });
    const s = at(5);
    expect(threadOf(s, G1).seq).toBe(5);
    for (const v of HOSTILE) expect(threadOf(at(v, s), G1).seq).toBe(5);
    // a hostile time in a change still lands the note, drawn as an unknown time
    const t = notesReduce(s, {
      type: "page",
      grantId: G1,
      page: { reset: false, seq: 6, changes: [{ seq: 6, note_id: note("z").note_id, note: note("z", { created_at: 1e300 }) }], more: false },
    });
    expect(threadOf(t, G1).notes.map((n) => [n.note_id[0], n.created_at])).toEqual([["z", 0]]);
  });
  test("a request card, a decision list and an access status with hostile numbers never throw", () => {
    for (const v of HOSTILE) {
      expect(
        pendingAccess([
          { request_id: "r".repeat(22), level: "send", state: "pending", note: "n", peer_name: v, created_at: v, expires_at: v },
        ]),
      ).toHaveLength(1);
      expect(() =>
        cleanDecisions([{ request_id: "q1", turn_id: v, answer: "once", author: { via: "peer", name: v }, mine: v, at: v }]),
      ).not.toThrow();
      expect(
        cleanAccessStatus({ level: "view", request: { request_id: "r".repeat(22), level: "send", state: "pending", created_at: v } }),
      ).not.toBeNull();
    }
    // A list longer than the owner may send sizes nothing here.
    const many = Array.from({ length: 500 }, (_, i) => ({ request_id: `q${i}`, turn_id: "t", answer: "once", author: { via: "owner" } }));
    expect(cleanDecisions(many)).toHaveLength(64);
  });
  test("a request's questions and options are drawn up to a bound, whatever the list says", () => {
    const r = cleanRequest({
      request_id: "q1",
      turn_id: "t1",
      kind: "question",
      questions: Array.from({ length: 100 }, () => ({ question: "?", options: Array.from({ length: 500 }, (_, i) => `o${i}`) })),
    })!;
    expect(r.questions).toHaveLength(16);
    expect(r.questions![0].options).toHaveLength(64);
  });
});

// ---- a terminal share (#2060): none of working together applies ---------------------------------

describe("a terminal grant", () => {
  test("has no row in the owner's strip, and an API grant does", () => {
    const term = grant({ grant_id: G2, kind: "terminal", peer_name: "render-box" });
    expect(stripState([term], NOW).rows).toEqual([]);
    expect(stripState([grant({ kind: "api" }), term], NOW).rows.map((r) => r.grantId)).toEqual([G1]);
    // a grant from a build before kinds is an API grant
    expect(stripState([grant()], NOW).rows).toHaveLength(1);
  });
  test("never names a peer who may answer a decision, even a row that claims send", () => {
    expect(sendPeers([grant({ kind: "terminal", level: "send" })], NOW)).toEqual([]);
    expect(sendPeers([grant({ kind: "api", level: "send" })], NOW)).toEqual(["gpu-box"]);
  });
});
