/** The owner's side of suggest and send (#2061): the dialog's levels and the suggestion cards,
 *  as pure mapping. What must never happen is provable here: Send shared without the
 *  acknowledgement of that exact choice, a state shown as another, an outcome invented. */
import { describe, expect, test } from "vitest";
import {
  GRANT_BUSY,
  GRANT_LEVELS,
  LEVEL_EXPIRIES,
  LEVEL_PRESELECTED,
  LEVEL_TILES,
  MISSION_HELD_LINE,
  SEND_ACK,
  SUGGESTION_ALREADY_APPROVED,
  SUGGESTION_ALREADY_REJECTED,
  SUGGESTION_APPROVING,
  SUGGESTION_EXPIRED,
  SUGGESTION_NOTE,
  SUGGESTION_NOTE_MS,
  SUGGESTION_SHARE_ENDED,
  SUGGEST_LINE,
  ackKey,
  acknowledged,
  canShare,
  cleanSuggestion,
  decisionOutcome,
  expiryFor,
  isGrantBusy,
  isHeldRefusal,
  isPeerSuggestionNotice,
  peerSuggestionTitle,
  sendEndsText,
  sendRiskText,
  shareRequest,
  suggestionCardView,
  suggestionCardsInit,
  suggestionCardsReduce,
  suggestionPoll,
  waitingCount,
  type PeerSuggestion,
  type SuggestionCardsState,
} from "./federationInput";

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const RENDER = "c4vy2sxqe7kdm3nzh6pbw5taj2ruo7gf";

describe("the levels of the share dialog", () => {
  test("View is preselected, never Send; the tiles say what each level means", () => {
    expect(LEVEL_PRESELECTED).toBe("view");
    expect(GRANT_LEVELS).toEqual(["view", "suggest", "send"]);
    expect(LEVEL_TILES.suggest).toEqual({ name: "Suggest", desc: "Proposes messages. You approve each one." });
    expect(LEVEL_TILES.send).toEqual({ name: "Send", desc: "Messages go straight to the agent." });
  });

  test("Suggest offers 1, 7 (default) and 30 days; Send 1 hour (default), 4 and 24 hours", () => {
    expect(LEVEL_EXPIRIES.suggest.map((o) => o.label)).toEqual(["1 day", "7 days", "30 days"]);
    expect(expiryFor("suggest", null).seconds).toBe(7 * 86400);
    expect(LEVEL_EXPIRIES.send.map((o) => o.label)).toEqual(["1 hour", "4 hours", "24 hours"]);
    expect(LEVEL_EXPIRIES.send.map((o) => o.seconds)).toEqual([3600, 4 * 3600, 24 * 3600]);
    expect(expiryFor("send", null).seconds).toBe(3600);
    expect(LEVEL_EXPIRIES.view.map((o) => o.label)).toEqual(["1 day", "7 days", "30 days", "90 days"]);
  });

  test("an expiry picked for one level never follows to a level that does not offer it", () => {
    expect(expiryFor("send", 90 * 86400).seconds).toBe(3600); // View's 90 days → Send's default
    expect(expiryFor("suggest", 90 * 86400).seconds).toBe(7 * 86400);
    expect(expiryFor("view", 4 * 3600).seconds).toBe(7 * 86400);
    expect(expiryFor("send", 4 * 3600).seconds).toBe(4 * 3600);
    expect(expiryFor("suggest", 86400).seconds).toBe(86400); // one both offer is kept
  });

  test("the copy", () => {
    expect(SUGGEST_LINE).toBe(
      "A suggestion reaches the agent only after you approve that one message. Nothing approves it for you.",
    );
    expect(sendRiskText("gpu-box")).toBe(
      "Send lets gpu-box's operator instruct this agent directly. Whatever this agent can do on this machine, their messages can ask it to do.",
    );
    expect(SEND_ACK).toBe("I understand and want to allow this.");
    expect(sendEndsText("1 hour")).toBe("It ends by itself after 1 hour. Only you can renew it.");
    expect(MISSION_HELD_LINE).toBe("This session belongs to a mission right now. It can only be watched.");
    expect(GRANT_BUSY).toBe("A message is being delivered. Nothing changed. Try again.");
  });
});

describe("the acknowledgement belongs to one choice", () => {
  const ticked = ackKey(GPU, "send");

  test("it holds for that peer at Send, and for nothing else", () => {
    expect(acknowledged(ticked, GPU, "send")).toBe(true);
    expect(acknowledged(ticked, RENDER, "send")).toBe(false); // another peer
    expect(acknowledged(ticked, GPU, "suggest")).toBe(false); // another level
    expect(acknowledged(null, GPU, "send")).toBe(false); // a reopened dialog starts with none
    expect(acknowledged(ackKey(GPU, "suggest"), GPU, "send")).toBe(false);
    expect(acknowledged(ackKey(null, "send"), null, "send")).toBe(true);
  });

  test("Share: Send needs the tick; View and Suggest do not; no peer, no share", () => {
    const base = { peerId: GPU, seconds: 3600, missionHeld: false };
    expect(canShare({ ...base, level: "send", ackFor: null })).toBe(false);
    expect(canShare({ ...base, level: "send", ackFor: ackKey(RENDER, "send") })).toBe(false);
    expect(canShare({ ...base, level: "send", ackFor: ticked })).toBe(true);
    expect(canShare({ ...base, level: "view", ackFor: null })).toBe(true);
    expect(canShare({ ...base, level: "suggest", ackFor: null })).toBe(true);
    expect(canShare({ ...base, peerId: null, level: "view", ackFor: null })).toBe(false);
  });

  test("the request carries accept_risk: true only for Send, only when ticked; never false", () => {
    const base = { peerId: GPU, seconds: 4 * 3600, missionHeld: false };
    expect(shareRequest({ ...base, level: "send", ackFor: null })).toBeNull();
    expect(shareRequest({ ...base, level: "send", ackFor: ticked })).toEqual({
      level: "send",
      expires_in: 4 * 3600,
      accept_risk: true,
    });
    for (const level of ["view", "suggest"] as const) {
      // a tick left over from Send does not ride along with another level
      const body = shareRequest({ ...base, level, seconds: 86400, ackFor: ticked });
      expect(body).toEqual({ level, expires_in: 86400 });
      expect(body).not.toHaveProperty("accept_risk");
    }
    // an expiry Send does not offer is never sent for Send
    expect(shareRequest({ ...base, level: "send", seconds: 90 * 86400, ackFor: ticked })?.expires_in).toBe(3600);
  });

  test("a mission-held session can only be shared to View", () => {
    const base = { peerId: GPU, seconds: 3600, missionHeld: true };
    expect(canShare({ ...base, level: "view", ackFor: null })).toBe(true);
    expect(canShare({ ...base, level: "suggest", ackFor: null })).toBe(false);
    expect(canShare({ ...base, level: "send", ackFor: ticked })).toBe(false);
    expect(shareRequest({ ...base, level: "send", ackFor: ticked })).toBeNull();
  });

  test("503 busy is its own refusal; so is 409 held", () => {
    expect(isGrantBusy(503, { code: "busy" })).toBe(true);
    expect(isGrantBusy(503, { code: "unreadable" })).toBe(false);
    expect(isGrantBusy(409, { code: "busy" })).toBe(false);
    expect(isGrantBusy(503, null)).toBe(false);
    expect(isHeldRefusal(409, { code: "held" })).toBe(true);
    expect(isHeldRefusal(409, { code: "conflict" })).toBe(false);
  });
});

const SID = "sAbCdEfGhIjKlMnOpQrStU";
const suggestion = (over: Partial<PeerSuggestion> = {}): PeerSuggestion => ({
  suggestion_id: SID,
  grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
  session_key: "codex-api:x",
  peer_id: GPU,
  peer_name: "gpu-box",
  actor: "",
  text: "Please keep the old entry point.",
  state: "pending",
  approved_text: null,
  approved_by: null,
  approved_at: null,
  edited: false,
  handoff: null,
  end_reason: null,
  created_at: 100,
  updated_at: 100,
  expires_at: 100 + 86400,
  ...over,
});

describe("a suggestion card: one presentation per state", () => {
  test("pending waits; approving is decided but not delivered; approved and rejected leave", () => {
    expect(suggestionCardView(suggestion())).toEqual({ kind: "pending" });
    const approving = suggestionCardView(suggestion({ state: "approving" }));
    expect(approving).toEqual({ kind: "approving", text: "Approved · delivering…" });
    expect(SUGGESTION_APPROVING).not.toMatch(/delivered|sent\b/i);
    expect(suggestionCardView(suggestion({ state: "approved" }))).toEqual({ kind: "none" });
    expect(suggestionCardView(suggestion({ state: "rejected" }))).toEqual({ kind: "none" });
  });

  test("withdrawn says by whom; expired is expired, never rejected", () => {
    expect(suggestionCardView(suggestion({ state: "withdrawn", end_reason: "peer" }))).toEqual({
      kind: "note",
      text: "gpu-box withdrew this suggestion.",
      word: "withdrawn",
    });
    for (const end_reason of ["grant_revoked", "grant_expired", "grant_ended", "mission_held", null])
      expect(suggestionCardView(suggestion({ state: "withdrawn", end_reason }))).toEqual({
        kind: "note",
        text: "This suggestion was withdrawn because the share ended.",
        word: "withdrawn",
      });
    const expired = suggestionCardView(suggestion({ state: "expired", end_reason: "expired" }));
    expect(expired).toEqual({ kind: "note", text: "This suggestion expired.", word: "expired" });
    expect(SUGGESTION_EXPIRED).not.toMatch(/reject/i);
  });

  test("each 409 shows what the server says happened", () => {
    const conflict = (state: string) => ({ code: "conflict", state });
    const withdrawn = suggestion({ state: "withdrawn", end_reason: "peer" });
    expect(decisionOutcome(409, conflict("withdrawn"), withdrawn)).toEqual({
      text: "gpu-box withdrew this suggestion.",
      word: "withdrawn",
    });
    expect(decisionOutcome(409, conflict("withdrawn"), suggestion({ state: "withdrawn", end_reason: "grant_revoked" }))).toEqual({
      text: SUGGESTION_SHARE_ENDED,
      word: "withdrawn",
    });
    // without the row the 409 says "withdrawn" and not by whom: nobody is named
    expect(decisionOutcome(409, conflict("withdrawn"), null)).toEqual({
      text: "This suggestion was withdrawn.",
      word: "withdrawn",
    });
    expect(decisionOutcome(409, { code: "ended", state: "pending" }, null)).toEqual({
      text: SUGGESTION_SHARE_ENDED,
      word: "withdrawn",
    });
    expect(decisionOutcome(409, { code: "ended" }, suggestion())).toEqual({
      text: SUGGESTION_SHARE_ENDED,
      word: "withdrawn",
    });
    for (const state of ["approved", "approving"])
      expect(decisionOutcome(409, conflict(state), null)).toEqual({
        text: SUGGESTION_ALREADY_APPROVED,
        word: "approved",
      });
    expect(decisionOutcome(409, conflict("rejected"), null)).toEqual({
      text: SUGGESTION_ALREADY_REJECTED,
      word: "rejected",
    });
    expect(decisionOutcome(409, conflict("expired"), null)).toEqual({
      text: SUGGESTION_EXPIRED,
      word: "expired",
    });
    // the row as the list shows it NOW wins over the 409's word
    expect(decisionOutcome(409, conflict("approving"), suggestion({ state: "approved" }))?.word).toBe("approved");
  });

  test("what is not an outcome is not turned into one", () => {
    expect(decisionOutcome(409, { code: "held" }, suggestion())).toBeNull(); // nothing was decided
    expect(decisionOutcome(503, { code: "busy" }, null)).toBeNull();
    expect(decisionOutcome(504, { code: "timeout" }, null)).toBeNull();
    expect(decisionOutcome(0, null, null)).toBeNull();
    expect(decisionOutcome(409, { code: "conflict" }, null)).toBeNull();
    expect(decisionOutcome(409, { code: "conflict", state: "pending" }, suggestion())).toBeNull();
  });
});

describe("the cards above the composer", () => {
  const list = (state: SuggestionCardsState, rows: unknown, now = 1000) =>
    suggestionCardsReduce(state, { type: "list", rows, now });
  const ids = (s: SuggestionCardsState) => s.cards.map((c) => c.suggestion.suggestion_id);
  const B = "tAbCdEfGhIjKlMnOpQrStU";

  test("pending ones, oldest first, with their count", () => {
    const s = list(suggestionCardsInit, [
      suggestion({ suggestion_id: B, created_at: 200 }),
      suggestion({ created_at: 100 }),
    ]);
    expect(ids(s)).toEqual([SID, B]);
    expect(waitingCount(s)).toBe(2);
    expect(SUGGESTION_NOTE).toBe("Nothing is sent until you approve. It is not shown to the agent.");
  });

  test("old, long-settled suggestions in the list never become cards", () => {
    const s = list(suggestionCardsInit, [
      suggestion({ state: "approved" }),
      suggestion({ suggestion_id: B, state: "withdrawn", end_reason: "peer" }),
      suggestion({ suggestion_id: "u".repeat(22), state: "expired" }),
      suggestion({ suggestion_id: "v".repeat(22), state: "rejected" }),
    ]);
    expect(s.cards).toEqual([]);
  });

  test("a card seen waiting says what happened to it, then leaves", () => {
    let s = list(suggestionCardsInit, [suggestion()]);
    s = list(s, [suggestion({ state: "withdrawn", end_reason: "peer" })], 2000);
    expect(s.cards[0].view).toEqual({ kind: "note", text: "gpu-box withdrew this suggestion.", word: "withdrawn" });
    expect(waitingCount(s)).toBe(0);
    // it stays through later reads, and leaves on its own clock
    s = list(s, [suggestion({ state: "withdrawn", end_reason: "peer" })], 3000);
    expect(s.cards).toHaveLength(1);
    expect(suggestionCardsReduce(s, { type: "tick", now: 2000 + SUGGESTION_NOTE_MS - 1 })).toBe(s);
    expect(suggestionCardsReduce(s, { type: "tick", now: 2000 + SUGGESTION_NOTE_MS }).cards).toEqual([]);

    let e = list(suggestionCardsInit, [suggestion()]);
    e = list(e, [suggestion({ state: "expired" })]);
    expect(e.cards[0].view).toMatchObject({ kind: "note", text: "This suggestion expired.", word: "expired" });
  });

  test("approved elsewhere or rejected: the card just leaves; approving stays and says so", () => {
    const one = list(suggestionCardsInit, [suggestion()]);
    expect(list(one, [suggestion({ state: "approved" })]).cards).toEqual([]);
    expect(list(one, [suggestion({ state: "rejected" })]).cards).toEqual([]);
    const approving = list(one, [suggestion({ state: "approving" })]);
    expect(approving.cards[0].view).toEqual({ kind: "approving", text: "Approved · delivering…" });
    expect(approving.cards[0].leavesAt).toBeNull();
    expect(waitingCount(approving)).toBe(0);
    // a suggestion first seen while it is being delivered is shown too
    expect(list(suggestionCardsInit, [suggestion({ state: "approving" })]).cards).toHaveLength(1);
  });

  test("a decision's own answer: approved leaves at once, approving stays", () => {
    const one = list(suggestionCardsInit, [suggestion(), suggestion({ suggestion_id: B, created_at: 300 })]);
    const done = suggestionCardsReduce(one, { type: "decided", suggestion: suggestion({ state: "approved" }) });
    expect(ids(done)).toEqual([B]); // exactly the decided one; the other is untouched
    const wait = suggestionCardsReduce(one, { type: "decided", suggestion: suggestion({ state: "approving" }) });
    expect(wait.cards[0].view.kind).toBe("approving");
    expect(suggestionCardsReduce(one, { type: "decided", suggestion: null })).toBe(one);
  });

  test("a 409's outcome is shown on that card only, and survives the next read", () => {
    const one = list(suggestionCardsInit, [suggestion(), suggestion({ suggestion_id: B, created_at: 300 })]);
    let s = suggestionCardsReduce(one, { type: "outcome", id: SID, text: "Already approved.", word: "approved", now: 5000 });
    expect(s.cards[0].view).toEqual({ kind: "note", text: "Already approved.", word: "approved" });
    expect(s.cards[1].view).toEqual({ kind: "pending" });
    s = list(s, [suggestion({ state: "approved" }), suggestion({ suggestion_id: B, created_at: 300 })], 6000);
    expect(s.cards.map((c) => c.view.kind)).toEqual(["note", "pending"]);
    expect(suggestionCardsReduce(s, { type: "tick", now: 5000 + SUGGESTION_NOTE_MS }).cards).toHaveLength(1);
  });

  test("a card no longer listed is dropped without a claim; another session resets", () => {
    const one = list(suggestionCardsInit, [suggestion()]);
    expect(list(one, []).cards).toEqual([]);
    expect(list(one, "nonsense").cards).toEqual([]);
    expect(suggestionCardsReduce(one, { type: "reset" }).cards).toEqual([]);
  });

  test("an entry is rebuilt field by field; what is not a suggestion is not listed", () => {
    expect(cleanSuggestion({ ...suggestion(), text: 7, peer_name: { a: 1 }, edited: "yes", extra: "x" })).toEqual(
      suggestion({ text: "", peer_name: "", edited: false }),
    );
    for (const bad of [null, "x", [], { state: "pending" }, { suggestion_id: "../x", state: "pending" }])
      expect(cleanSuggestion(bad)).toBeNull();
  });

  test("the list is polled while a card is open or the session has a Suggest or Send grant", () => {
    const open = list(suggestionCardsInit, [suggestion()]);
    expect(suggestionPoll(open, [])).toBe(true);
    expect(suggestionPoll(suggestionCardsInit, [])).toBe(false);
    expect(suggestionPoll(suggestionCardsInit, [{ level: "view" }])).toBe(false);
    expect(suggestionPoll(suggestionCardsInit, [{ level: "view" }, { level: "suggest" }])).toBe(true);
    expect(suggestionPoll(suggestionCardsInit, [{ level: "send" }])).toBe(true);
  });
});

describe("the bell's entry", () => {
  test("names the peer and the session, from the row's two fields", () => {
    const row = { action_id: `peer-suggestion:${SID}`, title: "gpu-box suggests a message", project: "Refactor the importer" };
    expect(isPeerSuggestionNotice(row)).toBe(true);
    expect(peerSuggestionTitle(row)).toBe("gpu-box suggests a message · Refactor the importer");
    expect(peerSuggestionTitle({ title: "gpu-box suggests a message", project: "" })).toBe("gpu-box suggests a message");
    expect(isPeerSuggestionNotice({ action_id: "automation:x:1" })).toBe(false);
    expect(isPeerSuggestionNotice({})).toBe(false);
  });
});

describe("a terminal is shared View only (#2060): the levels are an API session's", () => {
  const choice = { peerId: "gpu", seconds: 86400, ackFor: null as string | null, missionHeld: false };
  test("Suggest and Send can never be the request for a terminal, ticked or not", () => {
    for (const level of ["suggest", "send"] as const) {
      const ticked = { ...choice, level, ackFor: ackKey("gpu", level), kind: "terminal" as const };
      expect(canShare(ticked), level).toBe(false);
      expect(shareRequest(ticked), level).toBeNull();
    }
  });
  test("View for a terminal is the plain request: no accept_risk, the view expiries", () => {
    const body = shareRequest({ ...choice, level: "view", kind: "terminal" });
    expect(body).toEqual({ level: "view", expires_in: 86400 });
    expect(body && "accept_risk" in body).toBe(false);
    // an API session is unchanged
    expect(shareRequest({ ...choice, level: "suggest", kind: "api" })).toEqual({ level: "suggest", expires_in: 86400 });
  });
});
