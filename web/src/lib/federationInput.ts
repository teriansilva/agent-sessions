/** The OWNER's side of suggest and send (#2061): the levels of the share dialog and the
 *  suggestion cards. Pure state mapping; the components only draw what these return.
 *
 *  Two rules live here because they are the security of the feature and must be testable without
 *  a browser: *Send* is never the dialog's choice unless the operator made it AND ticked the
 *  acknowledgement for that exact choice; and a suggestion is one card with one decision, whose
 *  every state has its own words (approving is not delivered, expired is not rejected). */
import type { FederationGrant, ShareKind } from "./federation";

export type GrantLevel = "view" | "suggest" | "send";
export const GRANT_LEVELS: readonly GrantLevel[] = ["view", "suggest", "send"];
/** What the dialog opens on, always: also for a session that already has a Send grant. */
export const LEVEL_PRESELECTED: GrantLevel = "view";

export const LEVEL_TILES: Record<GrantLevel, { name: string; desc: string }> = {
  view: { name: "View", desc: "Read the conversation" },
  suggest: { name: "Suggest", desc: "Proposes messages. You approve each one." },
  send: { name: "Send", desc: "Messages go straight to the agent." },
};

const DAY = 86400;
const HOUR = 3600;

export interface ExpiryOption {
  seconds: number;
  label: string;
  /** As it reads inside a sentence: "1 hour", "7 days". */
  span: string;
}
const days = (n: number): ExpiryOption => ({
  seconds: n * DAY,
  label: n === 1 ? "1 day" : `${n} days`,
  span: n === 1 ? "1 day" : `${n} days`,
});
const hours = (n: number): ExpiryOption => ({
  seconds: n * HOUR,
  label: n === 1 ? "1 hour" : `${n} hours`,
  span: n === 1 ? "1 hour" : `${n} hours`,
});

/** The expiry choices per level. Send is short on purpose and renewed only by the operator; the
 *  server's own bounds (`grants.MAX_EXPIRY_BY_LEVEL`) are at least these. */
export const LEVEL_EXPIRIES: Record<GrantLevel, readonly ExpiryOption[]> = {
  view: [days(1), days(7), days(30), days(90)],
  suggest: [days(1), days(7), days(30)],
  send: [hours(1), hours(4), hours(24)],
};
export const LEVEL_EXPIRY_DEFAULT: Record<GrantLevel, number> = {
  view: 7 * DAY,
  suggest: 7 * DAY,
  send: HOUR,
};

/** The expiry a level shows: the picked one when that level offers it, else the level's default.
 *  So switching from View (90 days) to Send can never carry 90 days along. */
export function expiryFor(level: GrantLevel, picked: number | null): ExpiryOption {
  const options = LEVEL_EXPIRIES[level];
  return (
    options.find((o) => o.seconds === picked) ??
    options.find((o) => o.seconds === LEVEL_EXPIRY_DEFAULT[level])!
  );
}

export const SUGGEST_LINE =
  "A suggestion reaches the agent only after you approve that one message. Nothing approves it for you.";
export const sendRiskText = (peer: string) =>
  `Send lets ${peer}'s operator instruct this agent directly. Whatever this agent can do on` +
  " this machine, their messages can ask it to do.";
export const SEND_ACK = "I understand and want to allow this.";
export const sendEndsText = (span: string) =>
  `It ends by itself after ${span}. Only you can renew it.`;
export const MISSION_HELD_LINE =
  "This session belongs to a mission right now. It can only be watched.";
export const GRANT_BUSY = "A message is being delivered. Nothing changed. Try again.";

/** The acknowledgement belongs to ONE choice: this peer at this level. `ackFor` is the choice
 *  the checkbox was ticked for; anything else (another peer, another level, a reopened dialog,
 *  which starts with `null`) is unticked. */
export const ackKey = (peerId: string | null, level: GrantLevel) => `${peerId ?? ""}/${level}`;
export function acknowledged(ackFor: string | null, peerId: string | null, level: GrantLevel): boolean {
  return level === "send" && ackFor !== null && ackFor === ackKey(peerId, level);
}

/** Whether Share can be pressed. Send needs the acknowledgement of this exact choice; a
 *  mission-held session offers View only, and so does a terminal (#2060: its screen, View only
 *  for good; the server refuses any other level for one). */
export function canShare(input: {
  peerId: string | null;
  level: GrantLevel;
  ackFor: string | null;
  missionHeld: boolean;
  kind?: ShareKind;
}): boolean {
  if (!input.peerId) return false;
  if ((input.missionHeld || input.kind === "terminal") && input.level !== "view") return false;
  if (input.level === "send") return acknowledged(input.ackFor, input.peerId, input.level);
  return true;
}

/** The body of the grant request. `accept_risk: true` is in it ONLY for Send and only when the
 *  acknowledgement of this choice is ticked; it is never sent as `false`, and never for another
 *  level. `null` when the choice cannot be shared (the caller's button is disabled then). */
export function shareRequest(input: {
  peerId: string | null;
  level: GrantLevel;
  seconds: number;
  ackFor: string | null;
  missionHeld: boolean;
  kind?: ShareKind;
}): { level: GrantLevel; expires_in: number; accept_risk?: true } | null {
  if (!canShare(input)) return null;
  const body: { level: GrantLevel; expires_in: number; accept_risk?: true } = {
    level: input.level,
    expires_in: expiryFor(input.level, input.seconds).seconds,
  };
  if (input.level === "send") body.accept_risk = true;
  return body;
}

/** Whether a refused grant write is the fence being busy (`503 busy`, #2057): nothing changed. */
export function isGrantBusy(status: number, body: unknown): boolean {
  return status === 503 && (body as { code?: unknown } | null)?.code === "busy";
}

/** Whether the server refused because a mission holds the session (`409 held`). */
export function isHeldRefusal(status: number, body: unknown): boolean {
  return status === 409 && (body as { code?: unknown } | null)?.code === "held";
}

export const isInputGrant = (g: Pick<FederationGrant, "level">) =>
  g.level === "suggest" || g.level === "send";

// ---- the suggestion cards ---------------------------------------------------------------------

/** A suggestion as the operator API lists it (`inputs.view`). `text` is another party's words:
 *  rendered as plain text, never as Markdown, markup or a link. */
export interface PeerSuggestion {
  suggestion_id: string;
  grant_id: string;
  session_key: string;
  peer_id: string;
  peer_name: string;
  actor: string;
  text: string;
  state: string;
  approved_text: string | null;
  approved_by: string | null;
  approved_at: number | null;
  edited: boolean;
  handoff: string | null;
  end_reason: string | null;
  created_at: number;
  updated_at: number;
  expires_at: number;
}

export const SUGGESTION_NOTE = "Nothing is sent until you approve. It is not shown to the agent.";
export const SUGGESTION_APPROVING = "Approved · delivering…";
export const SUGGESTION_EXPIRED = "This suggestion expired.";
export const SUGGESTION_SHARE_ENDED = "This suggestion was withdrawn because the share ended.";
export const SUGGESTION_ALREADY_APPROVED = "Already approved.";
export const SUGGESTION_ALREADY_REJECTED = "Already rejected.";
export const suggestionWithdrawnBy = (peer: string) => `${peer} withdrew this suggestion.`;
export const suggestionPeer = (s: Pick<PeerSuggestion, "peer_name" | "peer_id">) =>
  s.peer_name || s.peer_id.slice(0, 8) || "A paired instance";
/** How long a card that says what happened stays before it leaves. */
export const SUGGESTION_NOTE_MS = 6_000;
/** How often the owner's pane reads the list while it has reason to (see `suggestionPoll`). */
export const SUGGESTION_POLL_MS = 3_000;

export type SuggestionCardView =
  /** Waiting for the operator: the text and Approve · Edit · Reject. Amber, the word "waiting". */
  | { kind: "pending" }
  /** The decision is made, the delivery is not confirmed yet. No buttons. Never "delivered". */
  | { kind: "approving"; text: string }
  /** What happened to it, in grey, before the card leaves. */
  | { kind: "note"; text: string; word: string }
  /** Nothing to show: decided here (approved, rejected), or long over. */
  | { kind: "none" };

/** What a withdrawn or expired suggestion says. The peer took it back (`end_reason: "peer"`), or
 *  it went with the share (a grant that ended or was lowered, or anything else the owner did). */
export function endedNote(s: Pick<PeerSuggestion, "state" | "end_reason" | "peer_name" | "peer_id">): string {
  if (s.state === "expired") return SUGGESTION_EXPIRED;
  return s.end_reason === "peer" ? suggestionWithdrawnBy(suggestionPeer(s)) : SUGGESTION_SHARE_ENDED;
}

/** One presentation per state (#2061's table, the owner's column). */
export function suggestionCardView(s: PeerSuggestion): SuggestionCardView {
  switch (s.state) {
    case "pending":
      return { kind: "pending" };
    case "approving":
      return { kind: "approving", text: SUGGESTION_APPROVING };
    case "withdrawn":
      return { kind: "note", text: endedNote(s), word: "withdrawn" };
    case "expired":
      return { kind: "note", text: endedNote(s), word: "expired" };
    default:
      return { kind: "none" }; // approved, rejected: the card leaves
  }
}

/** What the card says when the server refused a decision. `409` means the other side or another
 *  tab decided first: the answer names the state that won (`state`) or that the share ended
 *  (`code: "ended"`). `row` is the suggestion as the list shows it NOW, when it could be read:
 *  it knows who withdrew it. `null` = not an outcome (an error to show, the card stays). */
export function decisionOutcome(
  status: number,
  body: unknown,
  row: PeerSuggestion | null,
): { text: string; word: string } | null {
  if (status !== 409) return null;
  const b = (body ?? {}) as { code?: unknown; state?: unknown };
  if (b.code === "held") return null; // a mission holds the session: nothing was decided
  const state = row && row.state !== "pending" ? row.state : typeof b.state === "string" ? b.state : null;
  if (b.code === "ended" && (!state || state === "pending" || state === "withdrawn"))
    return { text: SUGGESTION_SHARE_ENDED, word: "withdrawn" };
  switch (state) {
    case "approved":
    case "approving":
      return { text: SUGGESTION_ALREADY_APPROVED, word: "approved" };
    case "rejected":
      return { text: SUGGESTION_ALREADY_REJECTED, word: "rejected" };
    case "expired":
      return { text: SUGGESTION_EXPIRED, word: "expired" };
    case "withdrawn":
      return {
        text: row
          ? endedNote(row)
          : // The 409 says "withdrawn" and not by whom; without the row, only that much is said.
            "This suggestion was withdrawn.",
        word: "withdrawn",
      };
    default:
      return null;
  }
}
export const SUGGESTION_HELD =
  "This session belongs to a mission right now. Approve it when the mission has let it go.";

export interface SuggestionCard {
  suggestion: PeerSuggestion;
  view: Exclude<SuggestionCardView, { kind: "none" }>;
  /** For a note: when the card leaves (ms). */
  leavesAt: number | null;
}

export interface SuggestionCardsState {
  /** Oldest first. */
  cards: SuggestionCard[];
}

export type SuggestionCardsEvent =
  /** The list as the server answered it (any order). */
  | { type: "list"; rows: unknown; now: number }
  /** A decision's own answer said what happened to a card. */
  | { type: "outcome"; id: string; text: string; word: string; now: number }
  /** A decision succeeded: the card leaves at once (approved, rejected). */
  | { type: "decided"; suggestion: unknown }
  | { type: "tick"; now: number }
  /** Another session in the pane: nothing of the old one stays. */
  | { type: "reset" };

export const suggestionCardsInit: SuggestionCardsState = { cards: [] };

const SUGGESTION_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

/** A list entry rebuilt field by field: whatever the answer carries, a card only ever holds
 *  strings, numbers and booleans it knows. `null` for anything that is not a suggestion. */
export function cleanSuggestion(raw: unknown): PeerSuggestion | null {
  const r = raw as Record<string, unknown> | null;
  if (!r || typeof r !== "object" || Array.isArray(r)) return null;
  const id = str(r.suggestion_id);
  if (!SUGGESTION_ID_RE.test(id) || !str(r.state)) return null;
  return {
    suggestion_id: id,
    grant_id: str(r.grant_id),
    session_key: str(r.session_key),
    peer_id: str(r.peer_id),
    peer_name: str(r.peer_name),
    actor: str(r.actor),
    text: str(r.text),
    state: str(r.state),
    approved_text: typeof r.approved_text === "string" ? r.approved_text : null,
    approved_by: typeof r.approved_by === "string" ? r.approved_by : null,
    approved_at: typeof r.approved_at === "number" ? r.approved_at : null,
    edited: r.edited === true,
    handoff: typeof r.handoff === "string" ? r.handoff : null,
    end_reason: typeof r.end_reason === "string" ? r.end_reason : null,
    created_at: num(r.created_at),
    updated_at: num(r.updated_at),
    expires_at: num(r.expires_at),
  };
}

const oldestFirst = (a: SuggestionCard, b: SuggestionCard) =>
  a.suggestion.created_at - b.suggestion.created_at ||
  a.suggestion.suggestion_id.localeCompare(b.suggestion.suggestion_id);

/** The cards above the composer, from the list reads and the decisions' answers. Pure.
 *
 *  A suggestion that is waiting (or being delivered) always has a card. One that ended some
 *  other way (withdrawn, expired) gets its grey note ONLY if this pane saw it waiting: the list
 *  also returns old, long-settled suggestions, and those never appear. A note leaves after
 *  `SUGGESTION_NOTE_MS`; a card decided here leaves at once. */
export function suggestionCardsReduce(
  state: SuggestionCardsState,
  event: SuggestionCardsEvent,
): SuggestionCardsState {
  const live = (cards: SuggestionCard[], now: number) =>
    cards.filter((c) => c.leavesAt === null || c.leavesAt > now).sort(oldestFirst);
  if (event.type === "reset") return state.cards.length ? { cards: [] } : state;
  if (event.type === "tick") {
    const cards = live(state.cards, event.now);
    return cards.length === state.cards.length ? state : { cards };
  }
  if (event.type === "decided") {
    const done = cleanSuggestion(event.suggestion);
    if (!done) return state;
    const view = suggestionCardView(done);
    // `approving`: decided, not yet confirmed delivered. The card stays and says so.
    if (view.kind === "approving")
      return {
        cards: state.cards.map((c) =>
          c.suggestion.suggestion_id === done.suggestion_id
            ? { suggestion: done, view, leavesAt: null }
            : c,
        ),
      };
    return { cards: state.cards.filter((c) => c.suggestion.suggestion_id !== done.suggestion_id) };
  }
  if (event.type === "outcome") {
    return {
      cards: state.cards.map((c) =>
        c.suggestion.suggestion_id === event.id
          ? {
              ...c,
              view: { kind: "note", text: event.text, word: event.word },
              leavesAt: event.now + SUGGESTION_NOTE_MS,
            }
          : c,
      ),
    };
  }
  const rows = (Array.isArray(event.rows) ? event.rows : [])
    .map(cleanSuggestion)
    .filter(Boolean) as PeerSuggestion[];
  const byId = new Map(rows.map((r) => [r.suggestion_id, r]));
  const out: SuggestionCard[] = [];
  for (const card of state.cards) {
    const row = byId.get(card.suggestion.suggestion_id);
    byId.delete(card.suggestion.suggestion_id);
    if (card.view.kind === "note") {
      out.push(card); // it already says what happened; it leaves on its own clock
      continue;
    }
    if (!row) continue; // no longer listed: nothing is known of it, so nothing is claimed
    const view = suggestionCardView(row);
    if (view.kind === "none") continue;
    out.push({
      suggestion: row,
      view,
      leavesAt: view.kind === "note" ? event.now + SUGGESTION_NOTE_MS : null,
    });
  }
  for (const row of byId.values()) {
    const view = suggestionCardView(row);
    // First seen already over: it was never a card here, and does not become one.
    if (view.kind === "pending" || view.kind === "approving")
      out.push({ suggestion: row, view, leavesAt: null });
  }
  return { cards: live(out, event.now) };
}

/** How many are waiting for the operator (the count shown when it is more than one). */
export const waitingCount = (state: SuggestionCardsState) =>
  state.cards.filter((c) => c.view.kind === "pending").length;

/** Whether the pane keeps reading the list every `SUGGESTION_POLL_MS`: while a card is open, or
 *  while the session has a Suggest or Send grant (a suggestion can arrive at any moment). */
export function suggestionPoll(state: SuggestionCardsState, grants: readonly Pick<FederationGrant, "level">[]): boolean {
  return state.cards.length > 0 || grants.some(isInputGrant);
}

/** The bell's row for a pending suggestion (#2057 `notices.py`): the server's fixed title
 *  ("<peer> suggests a message") and the session's title. NEVER the suggested text: the row
 *  does not carry it, and this builds the line from those two fields alone. */
export const PEER_SUGGESTION_ACTION = "peer-suggestion:";
export const PEER_SUGGESTION_EVENT = "battlelab:peer-suggestion";
export function isPeerSuggestionNotice(n: { action_id?: string | null }): boolean {
  return typeof n.action_id === "string" && n.action_id.startsWith(PEER_SUGGESTION_ACTION);
}
export function peerSuggestionTitle(n: { title: string; project?: string | null }): string {
  return n.project ? `${n.title} · ${n.project}` : n.title;
}
