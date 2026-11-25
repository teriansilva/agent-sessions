/** Working together on a shared API session (#1246 P6, #2062): presence, asking for access, a
 *  decision both sides can see, the notes between the two operators and Continue here.
 *
 *  Pure logic and words only. Everything that arrives from another instance (a name, a note, a
 *  request's summary, an option's label, a seed) is rebuilt here field by field from `unknown`
 *  and is only ever handed to React as a text node: nothing in this module produces markup.
 *
 *  **Nothing here touches storage.** Notes, drafts, cursors, pending writes, a decision's
 *  attempt and a prepared brief live in component state; a reload forgets them and asks again.
 */
import type { FederationGrant } from "./federation";
import { grantEnds, isEpochS, remoteAccessWords, shareKind, writeFailure } from "./federation";
import type { GrantLevel } from "./federationInput";

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** A peer's change cursor or read mark: a whole number the server can be asked after again
 *  (`after` is bounded by `grants.REVISION_MAX`, 2^53). Anything else (`NaN`, `Infinity`,
 *  `1e300`, a negative, a string) is not a cursor: the caller keeps the one it had, so a
 *  hostile answer can neither stall the thread on a number the server refuses nor move it. */
export function cursorOf(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : fallback;
}

/** The most a count is shown as: past it the words say "999+". */
export const COUNT_SHOWN_MAX = 999;
/** A count from a read (how many notes a thread shows): a whole number, never negative, never a
 *  loop bound. Anything that is not one is 0. */
export function countOf(v: unknown): number {
  return typeof v === "number" && Number.isSafeInteger(v) && v > 0 ? v : 0;
}
/** At most this many questions of one request, and options of one question, are drawn: a
 *  peer's list never sizes the page. The owner bounds its answer too (`remote.check_snapshot`). */
export const QUESTIONS_SHOWN_MAX = 16;
export const OPTIONS_SHOWN_MAX = 64;
const obj = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const ID_RE = /^[A-Za-z0-9_-]{16,64}$/;

/** `<name>'s operator`: who is on the other side. */
export const operatorOf = (peer: string) => `${peer}'s operator`;

// ---- presence ---------------------------------------------------------------------------------

/** Beyond this many live grants the owner's strip is a count until it is opened. */
export const STRIP_ROWS_MAX = 2;
/** How often the owner's pane reads its grants (the `watching` bit lasts 30 s on the server). */
export const STRIP_POLL_MS = 10_000;

export interface StripRow {
  grantId: string;
  peerId: string;
  peer: string;
  /** "can view", "can suggest", "can send". */
  access: string;
  until: { text: string; countdown: boolean };
  watching: boolean;
  /** How many notes the thread with this peer shows. */
  notes: number;
  level: string;
}

export interface StripState {
  rows: StripRow[];
  /** More grants than the strip lists by default: it shows a count and opens on request. */
  collapsed: boolean;
  /** "Shared with 3 instances", for the collapsed strip and as the title over several rows. */
  summary: string;
}

/** "view only" is the receiver's word; the owner says what the peer CAN do. */
export function ownerAccessWords(level: string): string {
  return level === "send" ? "can send" : level === "suggest" ? "can suggest" : "can view";
}

/** The owner's strip, from the grants read. Live grants only, in the list's order. A peer's name
 *  and one bit: never a device, a count of viewers or a time. */
export function stripState(
  grants: readonly FederationGrant[],
  now: number,
  /** The operator's own choice (`true` listed, `false` a count); `null`: the default. */
  open: boolean | null = null,
): StripState {
  const rows = grants
    // A terminal grant is a screen to watch (#2060): none of working together applies to it, so
    // it has no row, no presence and no Notes here. An expiry that is not a date is not "ended":
    // the row says the date is unknown (`grantEnds`), the server says whether it is live.
    .filter((g) => g.state === "live" && shareKind(g) === "api" && !(isEpochS(g.expires_at) && g.expires_at <= now))
    .map((g) => ({
      grantId: g.grant_id,
      peerId: g.peer_id,
      peer: g.peer_name || "a paired instance",
      access: ownerAccessWords(g.level),
      until: grantEnds(g.expires_at, now),
      watching: g.watching === true,
      notes: countOf(g.notes),
      level: g.level,
    }));
  return {
    rows,
    // One grant is always its row. Several are listed up to two; beyond that, or when the
    // operator folded them, the strip is a count with one Notes control that asks which peer.
    collapsed: rows.length > 1 && !(open ?? rows.length <= STRIP_ROWS_MAX),
    summary: `Shared with ${rows.length} instances`,
  };
}

export const watchingLine = (peer: string) => `${peer} is watching`;
export const ownerHereLine = (owner: string) => `${operatorOf(owner)} is here`;
export const noteCountWords = (n: number) => {
  const count = countOf(n);
  if (count > COUNT_SHOWN_MAX) return `${COUNT_SHOWN_MAX}+ notes`;
  return count === 0 ? "no notes" : count === 1 ? "1 note" : `${count} notes`;
};

/** The peers of a session that hold a live `send` grant: each may answer a pending request. */
export function sendPeers(grants: readonly FederationGrant[], now: number): string[] {
  return grants
    .filter(
      (g) =>
        g.state === "live" &&
        g.level === "send" &&
        shareKind(g) === "api" &&
        !(isEpochS(g.expires_at) && g.expires_at <= now),
    )
    .map((g) => g.peer_name || "A paired instance");
}

/** The owner's extra line on a pending card while a peer could answer it. */
export function ownerRaceLine(peers: readonly string[]): string | null {
  if (peers.length === 0) return null;
  const who =
    peers.length === 1
      ? `${peers[0]} can send, so it`
      : `${peers.slice(0, -1).join(", ")} and ${peers[peers.length - 1]} can send, so they`;
  return `${who} may also approve this once or reject it. The first answer counts.`;
}

// ---- asking for access ------------------------------------------------------------------------

export const ACCESS_NOTE_MAX = 280;
export const ACCESS_POLL_MS = 5_000;
const LEVEL_ORDER: readonly GrantLevel[] = ["view", "suggest", "send"];

/** The levels a peer can ask for: the ones ABOVE what it has. None at `send`. */
export function askLevels(current: string | null | undefined): GrantLevel[] {
  const at = LEVEL_ORDER.indexOf(current as GrantLevel);
  return at < 0 ? [] : LEVEL_ORDER.slice(at + 1);
}

export const ASK_TILES: Record<"suggest" | "send", { name: string; desc: (owner: string) => string }> = {
  suggest: {
    name: "Suggest",
    desc: (owner) => `Propose messages. ${operatorOf(owner)} approves each one.`,
  },
  send: { name: "Send", desc: () => "Your messages go straight to the agent." },
};
export const askLead = (owner: string) =>
  `${operatorOf(owner)} decides. You can ask for one thing at a time, and withdraw it.`;

/** This instance's newest request as the owner reports it (`access.status`). */
export interface AccessStatus {
  level: string;
  request: { request_id: string; level: string; state: string } | null;
}

export function cleanAccessStatus(raw: unknown): AccessStatus | null {
  const o = obj(raw);
  if (typeof o.level !== "string") return null;
  const r = o.request === null || o.request === undefined ? null : obj(o.request);
  if (r && (!ID_RE.test(str(r.request_id)) || typeof r.state !== "string")) return null;
  return {
    level: o.level,
    request: r ? { request_id: str(r.request_id), level: str(r.level), state: str(r.state) } : null,
  };
}

export type AccessFoot =
  /** Nothing asked (or it was granted, withdrawn or expired): the ordinary foot. */
  | { kind: "none" }
  | { kind: "waiting"; text: string }
  | { kind: "declined"; text: string };

/** The remote pane's foot line for an access request. A request that was GRANTED shows nothing
 *  here: the banner carries the new level and the composer appears (#2061). One that asked for a
 *  level the grant already has is over, whatever its state says. */
export function accessFoot(status: AccessStatus | null, level: string | null, owner: string): AccessFoot {
  const r = status?.request;
  if (!r || askLevels(level).length === 0) return { kind: "none" };
  if (!askLevels(level).includes(r.level as GrantLevel)) return { kind: "none" };
  if (r.state === "pending")
    return { kind: "waiting", text: `Request sent · waiting for ${operatorOf(owner)}` };
  if (r.state === "declined") return { kind: "declined", text: `${operatorOf(owner)} declined.` };
  return { kind: "none" };
}

/** What a failed access request or withdrawal was (`access.request`, `access.withdraw`), by the
 *  shared classifier of a receiving write (`writeFailure`, #2061): no second table here.
 *  `ended`: the owner refused the call (`404 no_grant`); `offline`: it never left this instance;
 *  `rejected`: a proven refusal, nothing changed; `unconfirmed`: everything else (`504
 *  peer_lost`, a timeout, `429 busy`, the network, any answer nobody listed). After an
 *  unconfirmed one the pane says neither "sent" nor "not sent": it reads `access` again. */
export type AccessFailure = "ended" | "offline" | "rejected" | "unconfirmed";
export function accessFailure(status: number, code: unknown): AccessFailure {
  const failure = writeFailure(status, code);
  if (failure === "refused") return "ended";
  if (failure === "offline") return "offline";
  return failure === "unconfirmed" ? "unconfirmed" : "rejected";
}
export const ASK_UNCONFIRMED =
  "Could not confirm that the request arrived. This pane shows the request once it did. Ask again only if it does not.";
export const WITHDRAW_UNCONFIRMED =
  "Could not confirm that the request was withdrawn. The line above is what the owner reports.";

/** Whether an access status may replace the one a pane holds. A request only ever moves from
 *  `pending` to a final state (granted, declined, withdrawn, expired), and a new request has a
 *  new id. So an answer that says a request is `pending` after this pane was told that SAME
 *  request is over is an older answer, whatever order it arrived in (a status read cut before a
 *  Withdraw, landing after the Withdraw's own answer): it is dropped. `over`: the ids this
 *  pane has seen in a final state. */
export function accessStatusIsOlder(over: ReadonlySet<string>, next: AccessStatus | null): boolean {
  const r = next?.request;
  return !!r && r.state === "pending" && over.has(r.request_id);
}

/** A request for the owner's review, with the peer's note (plain text). */
export interface AccessRequest {
  request_id: string;
  grant_id: string;
  peer_id: string;
  peer_name: string;
  level: "suggest" | "send";
  note: string;
  state: string;
}

export function cleanAccessRequest(raw: unknown): AccessRequest | null {
  const o = obj(raw);
  const level = o.level;
  if (!ID_RE.test(str(o.request_id)) || (level !== "suggest" && level !== "send")) return null;
  return {
    request_id: str(o.request_id),
    grant_id: str(o.grant_id),
    peer_id: str(o.peer_id),
    peer_name: str(o.peer_name),
    level,
    note: str(o.note),
    state: str(o.state),
  };
}

/** The pending requests of a session, oldest first: the cards at the top of the share dialog. */
export function pendingAccess(raw: unknown): AccessRequest[] {
  const rows = Array.isArray(raw) ? raw : [];
  return rows
    .map(cleanAccessRequest)
    .filter((r): r is AccessRequest => r !== null && r.state === "pending")
    .reverse();
}

export const accessAsks = (peer: string, level: "suggest" | "send") =>
  `${peer} asks to be able to ${level} messages.`;
export const accessReviewHint = (level: "suggest" | "send") =>
  level === "send"
    ? "Review opens the controls below with Send chosen. The risk box stays unticked until you tick it."
    : "Review opens the controls below with Suggest chosen. Nothing is granted until you press Share.";

// ---- the bell ---------------------------------------------------------------------------------

/** Rows the server writes about a paired instance (`federation/notices.py`). Every one of them
 *  carries the peer's name, the session's title and a link: never a note, a request's note, a
 *  question or a suggested text. */
export const PEER_ACCESS_ACTION = "peer-access:";
export const PEER_NOTE_ACTION = "peer-note:";
/** The owner's row when a peer gave a share up (#2201): `<peer> left a share` and the title. */
export const PEER_LEFT_ACTION = "peer-left:";
const REMOTE_ACTION_RE = /^peer-remote-([a-z]{1,16}):([a-z2-7]{32}):([A-Za-z0-9_-]{16,64})$/;
/** The query that opens a thread: the owner's pane reads the grant id, the peer's pane any value. */
export const NOTES_QUERY = "notes";

export type PeerNotice =
  | { kind: "access"; label: string }
  | { kind: "left"; label: string }
  | { kind: "note"; label: string; grantId: string }
  | { kind: "remote"; label: string; what: string; peerId: string; grantId: string };

/** What a bell row is about, from its server-written action id; `null` for every other row. */
export function peerNotice(n: { action_id?: string | null }): PeerNotice | null {
  const id = n.action_id ?? "";
  if (id.startsWith(PEER_ACCESS_ACTION)) return { kind: "access", label: "Access request" };
  if (id.startsWith(PEER_LEFT_ACTION)) return { kind: "left", label: "Share ended" };
  if (id.startsWith(PEER_NOTE_ACTION)) {
    const grantId = id.slice(PEER_NOTE_ACTION.length);
    return ID_RE.test(grantId) ? { kind: "note", label: "Note", grantId } : null;
  }
  const m = REMOTE_ACTION_RE.exec(id);
  if (!m) return null;
  const label =
    m[1] === "note" ? "Note" : m[1] === "decision" ? "Decision" : m[1] === "ended" ? "Share ended" : "Shared session";
  return { kind: "remote", label, what: m[1], peerId: m[2], grantId: m[3] };
}

/** The row's one line: the server's sentence and the session's title. No other field is read. */
export function peerNoticeTitle(n: { title: string; project?: string | null }): string {
  return n.project ? `${n.title} · ${n.project}` : n.title;
}

// ---- decisions --------------------------------------------------------------------------------

export interface RemoteQuestion {
  question: string;
  multi: boolean;
  options: string[];
}

/** A pending request as a peer's projection carries it, rebuilt from `unknown`. */
export interface RemoteRequest {
  /** `<turn id>\0<request id>`: what a card, its attempt and its settlement are keyed by. */
  key: string;
  request_id: string;
  turn_id: string;
  kind: string;
  summary: string;
  /** The one-time kinds the OWNER says this instance may give. Empty: nothing may be answered. */
  choices: string[];
  /** The owner's name for THIS exact request (`offers.token`), sent back unchanged with an
   *  answer. `null` when the snapshot carried none or one of another shape: then nothing may be
   *  answered, whatever `choices` says. It identifies; it is not a secret. */
  offer: string | null;
  questions: RemoteQuestion[] | null;
}

const CHOICE_RE = /^[a-z][a-z_]{0,31}$/;
/** `offers.valid_token` on the server: 32 url-safe characters, nothing else. */
const OFFER_RE = /^[A-Za-z0-9_-]{32}$/;
export const validOffer = (raw: unknown): raw is string => typeof raw === "string" && OFFER_RE.test(raw);
export const decisionKeyOf = (turnId: string, requestId: string) => `${turnId}\u0000${requestId}`;

export function cleanRequest(raw: unknown): RemoteRequest | null {
  const o = obj(raw);
  const request_id = str(o.request_id);
  if (!request_id) return null;
  const turn_id = str(o.turn_id);
  const questions = Array.isArray(o.questions)
    ? o.questions.slice(0, QUESTIONS_SHOWN_MAX).map((q) => {
        const x = obj(q);
        return {
          question: str(x.question),
          multi: x.multi === true,
          options: Array.isArray(x.options) ? x.options.slice(0, OPTIONS_SHOWN_MAX).map(str) : [],
        };
      })
    : null;
  return {
    key: decisionKeyOf(turn_id, request_id),
    request_id,
    turn_id,
    kind: str(o.kind),
    summary: str(o.summary),
    choices: Array.isArray(o.choices)
      ? o.choices.filter((c): c is string => typeof c === "string" && CHOICE_RE.test(c))
      : [],
    offer: validOffer(o.offer) ? o.offer : null,
    questions,
  };
}

export function cleanRequests(raw: unknown): RemoteRequest[] {
  return (Array.isArray(raw) ? raw : []).map(cleanRequest).filter((r): r is RemoteRequest => r !== null);
}

/** Who settled a request, as every view of the session reads it (`share_api.decision_views`). */
export interface DecisionView {
  key: string;
  request_id: string;
  turn_id: string;
  /** The EFFECT of the answer: `once`, `decline`, `answer`, `standing`, `stop`. Never a label. */
  answer: string;
  author: { via: "owner" | "peer"; name: string };
  mine: boolean;
}

/** As many as the owner may send (`remote.DECISIONS_MAX`): a longer list sizes nothing here. */
const DECISIONS_MAX = 64;
export function cleanDecisions(raw: unknown): DecisionView[] {
  const out: DecisionView[] = [];
  for (const entry of Array.isArray(raw) ? raw.slice(0, DECISIONS_MAX) : []) {
    const o = obj(entry);
    const author = obj(o.author);
    const request_id = str(o.request_id);
    if (!request_id || !CHOICE_RE.test(str(o.answer))) continue;
    if (author.via !== "owner" && author.via !== "peer") continue;
    out.push({
      key: decisionKeyOf(str(o.turn_id), request_id),
      request_id,
      turn_id: str(o.turn_id),
      answer: str(o.answer),
      author: { via: author.via, name: str(author.name) },
      mine: o.mine === true,
    });
  }
  return out;
}

const isTool = (kind: string) => kind !== "command" && kind !== "file_change";

/** The button text for a choice: the app's own words (`choiceLabel` in `structuredView.ts`),
 *  restated here so this module needs nothing from the pane. A unit test holds the two equal. */
export function peerChoiceLabel(choice: string, kind: string, question: boolean): string {
  if (question) {
    if (choice === "answer") return "Send answer";
    if (choice === "reject") return "Skip question";
  }
  const tool = isTool(kind);
  if (choice === "approve") return tool ? "Allow once" : "Approve once";
  if (choice === "always") return tool ? "Allow always" : "Approve always";
  if (choice === "reject") return kind === "file_change" ? "Decline" : tool ? "Deny" : "Reject";
  return choice;
}

export function requestHead(kind: string, question: boolean, settled = false): string {
  if (question) return settled ? "The agent asked a question" : "The agent is waiting for an answer";
  const verb = settled ? "asked" : "asks";
  if (kind === "command") return `The agent ${verb} to run a command`;
  if (kind === "file_change") return `The agent ${verb} to change a file`;
  return `The agent ${verb} to use a tool`;
}

export interface DecisionButton {
  choice: string;
  label: string;
  /** False: rendered, disabled, and no handler answers it. */
  enabled: boolean;
  tone: "primary" | "ghost" | "danger";
}

export interface DecisionCard {
  /** `answer`: this instance may give a one-time answer. `watch`: it can only look. */
  role: "answer" | "watch";
  word: "you may answer" | "waiting";
  head: string;
  buttons: DecisionButton[];
  /** Under the buttons: why one of them is off, or that this card only waits. */
  line: string | null;
}

export const ownerOnlyLine = (owner: string) => `Only ${operatorOf(owner)} can allow this always.`;
export const DECISION_WAITING = "Waiting for an answer.";

/** A pending request's card on the RECEIVING side.
 *
 *  A peer with `send` may give what the owner lists in `choices` and nothing else. The option that
 *  widens what the agent may do without asking (`always`) is drawn, because the operator should
 *  see it exists, but it is **always disabled for a peer**: no data from anyone enables it, and
 *  the owner refuses it anyway. Below `send`, when the owner lists nothing, or when the request
 *  carries no well-formed `offer` to send back (the owner requires it: fail closed), the card
 *  has no control at all. */
export function decisionCard(request: RemoteRequest, level: string | null, owner: string): DecisionCard {
  const question = request.questions !== null;
  const head = requestHead(request.kind, question);
  const may = level === "send" && request.choices.length > 0 && request.offer !== null;
  if (!may) return { role: "watch", word: "waiting", head, buttons: [], line: DECISION_WAITING };
  const has = (c: string) => request.choices.includes(c);
  if (question)
    return {
      role: "answer",
      word: "you may answer",
      head,
      buttons: [
        { choice: "answer", label: peerChoiceLabel("answer", request.kind, true), enabled: has("answer"), tone: "primary" },
        { choice: "reject", label: peerChoiceLabel("reject", request.kind, true), enabled: has("reject"), tone: "ghost" },
      ],
      line: null,
    };
  return {
    role: "answer",
    word: "you may answer",
    head,
    buttons: [
      { choice: "approve", label: peerChoiceLabel("approve", request.kind, false), enabled: has("approve"), tone: "primary" },
      // Never enabled here, whatever `choices` says.
      { choice: "always", label: `${peerChoiceLabel("always", request.kind, false)} ▾`, enabled: false, tone: "ghost" },
      { choice: "reject", label: peerChoiceLabel("reject", request.kind, false), enabled: has("reject"), tone: "danger" },
    ],
    line: ownerOnlyLine(owner),
  };
}

/** The choices this card will ever send: a second gate behind the disabled attribute. */
export function mayGive(card: DecisionCard, choice: string): boolean {
  return card.role === "answer" && choice !== "always" && card.buttons.some((b) => b.choice === choice && b.enabled);
}

/** A question's picked options, one list of INDEXES per question. Complete when every question
 *  has at least one. The labels never leave this instance. */
export function answerOptions(questions: readonly RemoteQuestion[], picked: readonly (readonly number[])[]): number[][] | null {
  const out = questions.map((q, i) => {
    const mine = [...new Set(picked[i] ?? [])].filter((n) => Number.isInteger(n) && n >= 0 && n < q.options.length);
    return (q.multi ? mine : mine.slice(0, 1)).sort((a, b) => a - b);
  });
  return out.length > 0 && out.every((list) => list.length > 0) ? out : null;
}

/** One attempt at one request. A retry of the SAME answer to the SAME offer reuses its key and
 *  its offer (the owner then replays the stored decision); another answer, or the same answer to
 *  another offer (the request under that id is another one now), is another attempt and gets a
 *  new key. */
export interface DecisionAttempt {
  requestKey: string;
  answer: string;
  /** The snapshot's `offer` this attempt answers: sent with every delivery of it, unchanged. */
  offer: string;
  client_key: string;
}
export const answerSignature = (choice: string, options: number[][] | null) =>
  JSON.stringify({ choice, options: options ?? null });
export function attemptKey(
  held: DecisionAttempt | null | undefined,
  requestKey: string,
  answer: string,
  offer: string,
  mint: () => string,
): DecisionAttempt {
  return held && held.requestKey === requestKey && held.answer === answer && held.offer === offer
    ? held
    : { requestKey, answer, offer, client_key: mint() };
}

/** The choice and the picked options an attempt was made with (its `answerSignature`, read
 *  back). `null` for anything that is not one. */
export function attemptAnswer(attempt: Pick<DecisionAttempt, "answer">): { choice: string; options: number[][] | null } | null {
  try {
    const o = obj(JSON.parse(attempt.answer));
    if (typeof o.choice !== "string") return null;
    const options = Array.isArray(o.options) ? (o.options as number[][]) : null;
    return { choice: o.choice, options };
  } catch {
    return null;
  }
}

/** The ONE answer a card may still send while its last attempt is not confirmed: that attempt's
 *  own (same choice, same options, so the same key and offer). `null`: no such limit.
 *
 *  An unconfirmed answer may have been taken (`504 peer_lost`: the owner answered and the
 *  answer was lost). Another choice under a new key would then be a SECOND answer to a request
 *  that was already settled by the first; the owner replays a repeated decision id without a
 *  second effect, so the same press again is the only safe one until the snapshot settles it. */
export function lockedAnswer(
  state: { attempt: DecisionAttempt; outcome: DecisionOutcome | null } | null | undefined,
): { choice: string; options: number[][] | null } | null {
  if (!state || state.outcome?.kind !== "unconfirmed") return null;
  return attemptAnswer(state.attempt);
}

/** The attempt a next press may still reuse. After `not_pending` there is none: that offer names
 *  a request that is no longer waiting, so the next answer is to whatever the snapshot shows
 *  then, under a new key. */
export function heldAttempt(
  state: { attempt: DecisionAttempt; outcome: DecisionOutcome | null } | null | undefined,
): DecisionAttempt | null {
  if (!state) return null;
  return state.outcome?.kind === "refused" && state.outcome.code === "not_pending" ? null : state.attempt;
}

/** What a settled request reads as: the answer's effect in the card's own words. */
export function settledWord(answer: string, kind: string, question: boolean): string {
  if (question) return answer === "answer" ? "Answered" : answer === "decline" ? "Skipped" : "Stopped";
  const tool = isTool(kind);
  if (answer === "once") return tool ? "Allowed once" : "Approved once";
  if (answer === "standing") return tool ? "Allowed always" : "Approved always";
  if (answer === "decline") return kind === "file_change" ? "Declined" : tool ? "Denied" : "Rejected";
  if (answer === "stop") return "Rejected, and the turn was stopped";
  return "Answered";
}

/** Who answered, by the name THIS view shows them under. `side` says where the card is drawn:
 *  on the receiving side the owner is "<owner>'s operator" and this instance is "you". */
export function authorName(d: Pick<DecisionView, "author" | "mine">, side: "owner" | "peer", owner: string): string {
  if (side === "peer") {
    if (d.mine) return "you";
    return d.author.via === "owner" ? operatorOf(owner) : d.author.name || "another instance";
  }
  return d.author.via === "owner" ? "you" : d.author.name || "a paired instance";
}
export const answeredBy = (name: string) => `answered by ${name}`;
export const alreadyAnsweredBy = (name: string) => `Already answered by ${name}.`;

/** What the owner answered to ONE decision call. Nothing here is ever retried by the client. */
export type DecisionOutcome =
  /** This answer was taken. The snapshot's `decisions` will name this instance. */
  | { kind: "accepted" }
  /** Someone else's answer counted. Never retried. */
  | { kind: "lost"; text: string }
  /** No usable answer: it may have been taken. Retry sends the same attempt. */
  | { kind: "unconfirmed"; text: string }
  /** A refusal with its own meaning, shown in the card. */
  | { kind: "refused"; code: string; text: string }
  /** The grant no longer allows it: the pane's unavailable / offline state says so. */
  | { kind: "no_grant" }
  | { kind: "offline" };

export const DECISION_UNCONFIRMED =
  "Could not confirm that answer. It may have been taken. Retry sends the same answer, not a second one.";
export const decisionRetryLabel = (label: string) => `Retry: ${label}`;
export const DECISION_HELD = "This session is run by a mission right now.";
export const DECISION_CONFLICT = "You already answered this differently.";
export const DECISION_NOT_PENDING = "This request is no longer waiting.";

/** A decision call's 200 answer (`{outcome, author}`). */
export function decisionAnswered(raw: unknown, owner: string): DecisionOutcome {
  const o = obj(raw);
  if (o.outcome === "accepted") return { kind: "accepted" };
  if (o.outcome === "settled") {
    const a = obj(o.author);
    const name = a.via === "owner" ? operatorOf(owner) : str(a.name) || "another instance";
    return { kind: "lost", text: a.via ? alreadyAnsweredBy(name) : "Already answered." };
  }
  return { kind: "unconfirmed", text: DECISION_UNCONFIRMED };
}

/** A decision call that failed, from its HTTP status and the server's code.
 *
 *  Which failures are PROVEN refusals is the shared classifier's call (`writeFailure`, #2061):
 *  this function only gives each of them its words. Everything it does not prove (`504
 *  peer_lost`, `504 timeout`, `429 busy`, the network, a 5xx, any status or code nobody listed)
 *  is `unconfirmed`: the answer may have been taken. */
export function decisionFailed(status: number, code: unknown, owner: string): DecisionOutcome {
  const failure = writeFailure(status, code);
  if (failure === "unconfirmed") return { kind: "unconfirmed", text: DECISION_UNCONFIRMED };
  if (failure === "refused") return { kind: "no_grant" };
  if (failure === "offline") return { kind: "offline" };
  if (failure === "held") return { kind: "refused", code: "held", text: DECISION_HELD };
  if (status === 403)
    return { kind: "refused", code: "owner_only", text: `Only ${operatorOf(owner)} can allow this.` };
  if (status === 409 && code === "conflict")
    return { kind: "refused", code: "conflict", text: DECISION_CONFLICT };
  if (status === 409 && code === "not_pending")
    return { kind: "refused", code: "not_pending", text: DECISION_NOT_PENDING };
  return { kind: "refused", code: "invalid", text: "That answer was not taken." };
}

/** What a pane remembers of a request it SAW pending, so the settled card can still say what
 *  was asked. Memory only. */
export interface SeenRequest {
  kind: string;
  summary: string;
  question: string | null;
}
export const seenOf = (r: RemoteRequest): SeenRequest => ({
  kind: r.kind,
  summary: r.summary,
  question: r.questions ? (r.questions[0]?.question ?? "") : null,
});

/** How many settled cards a pane shows at most. */
export const SETTLED_SHOWN = 3;

/** The settled requests a pane shows: the ones it saw pending, and the ones of the newest turn
 *  (a pane that was just opened knows nothing older). Still-pending requests are never settled
 *  here, whatever a stale `decisions` list says. Newest last. */
export function settledShown(
  decisions: readonly DecisionView[],
  pendingKeys: ReadonlySet<string>,
  seen: ReadonlyMap<string, SeenRequest>,
  newestTurn: string | null | undefined,
): DecisionView[] {
  return decisions
    .filter((d) => !pendingKeys.has(d.key) && (seen.has(d.key) || (!!newestTurn && d.turn_id === newestTurn)))
    .slice(-SETTLED_SHOWN);
}

// ---- notes ------------------------------------------------------------------------------------

export const NOTE_MAX = 2000;
export const NOTES_POLL_MS = 4_000;
export const notesLine = (other: string) =>
  `Notes are between you and ${operatorOf(other)}. The agent never sees them.`;
export const NOTES_ENDED_PEER = "Notes ended with the share.";
export const NOTES_ENDED_OWNER = "You can read these for 7 more days.";
export const NOTE_UNCONFIRMED = "It may have been saved. Retry sends the same note, not a second one.";
export const NOTE_CHANGE_UNCONFIRMED = "Could not confirm that change. The thread shows what the other side has.";
export const NOTE_RATE = "You have sent many notes in the last hour. Try again later.";
export const NOTE_DELETED = "This note was deleted.";
export const NOTE_GONE = "That note is no longer in this thread. Nothing was sent again.";
export const notesOffline = (other: string) => `${other} is not connected. Nothing is shown.`;
export const noteCounter = (text: string) => `${[...text].length} / ${NOTE_MAX}`;
export const noteFits = (text: string) => text.trim().length > 0 && [...text].length <= NOTE_MAX;

export interface Note {
  note_id: string;
  author: "owner" | "peer";
  mine: boolean;
  text: string;
  /** When it was written, as the other instance says: a date (`isEpochS`) or 0, which reads as
   *  an unknown time (`shortTime`). Never handed to `Date` here. */
  created_at: number;
  /** `null`: never edited. Any other value means edited; a time that is not a date is 0. */
  edited_at: number | null;
  deleted: boolean;
  /** Owner's view only: hidden from the peer. */
  hidden: boolean;
}

export function cleanNote(raw: unknown): Note | null {
  const o = obj(raw);
  if (!ID_RE.test(str(o.note_id))) return null;
  return {
    note_id: str(o.note_id),
    author: o.author === "owner" ? "owner" : "peer",
    mine: o.mine === true,
    text: o.deleted === true ? "" : str(o.text).slice(0, NOTE_MAX * 4),
    created_at: isEpochS(o.created_at) ? o.created_at : 0,
    edited_at: o.edited_at === null || o.edited_at === undefined ? null : isEpochS(o.edited_at) ? o.edited_at : 0,
    deleted: o.deleted === true,
    hidden: o.hidden === true,
  };
}

/** A note this pane wrote that is not in the thread yet. `saved`: the server confirmed it, with
 *  an answer that names no change number, so its content waits for the next read (`confirmed`). */
export interface PendingNote {
  client_key: string;
  text: string;
  state: "sending" | "unconfirmed" | "saved";
  at: number;
}

/** `loading`: nothing read yet. `live`. `offline`: the other instance is not connected; nothing
 *  is shown. `gone`: the share ended (receiver): nothing is shown, for good. `ended`: the share
 *  ended (owner): the thread is read-only for seven days. */
export type ThreadStatus = "loading" | "live" | "offline" | "gone" | "ended";

export interface NoteThread {
  status: ThreadStatus;
  notes: Note[];
  /** The change cursor the next read asks after (receiver). */
  seq: number;
  draft: string;
  /** The retry key of the submission in the box: one per submission (`draftKey`). */
  held: { text: string; key: string } | null;
  pending: PendingNote[];
  /** A CONFIRMED failure of the last submission: nothing was saved. */
  refusal: string | null;
}

/** **THE ORDER RULE. A thread is only ever moved forward by the owner's change sequence.**
 *
 *  Note content (a note, an edit of one, its tombstone) and removals enter `notes` by exactly
 *  these doors, each ordered by the owner's change numbers:
 *
 *  1. `page` (the receiver's read): applied only if it was asked from the cursor the thread has.
 *  2. `thread` (the owner's whole read): applied only if its `seq` is not behind the one held.
 *  3. `confirmed` (an add's answer): inserts its note only when the answer carries a change
 *     number AHEAD of the cursor. At or behind the cursor a read has already consumed that
 *     change, and whatever came after it (an edit, the owner's hide): the answer then only
 *     settles the submission. An answer with no usable number never inserts.
 *
 *  **Nothing else inserts or replaces a note.** An edit's, a delete's and a hide's answer carry
 *  no change number, so there is NO event for them: the drawer reads the thread again and the
 *  result arrives by door 1 or 2. `missing` only ever takes a note away. So an answer that is
 *  late, by any amount, cannot put back a note whose removal the thread consumed, nor an older
 *  text over a newer one: a consumed removal is final for that id until a NEWER page says
 *  otherwise, because the cursor is past it and only a page asked from that cursor is applied.
 *
 *  Why an add that is AHEAD of the cursor is inserted, and the cursor is left where it is: the
 *  next read is asked from that same cursor, which is before the add, so it carries the add's
 *  change (or the note's removal, if the owner hid it since) and reconciles by note id through
 *  door 1. No duplicate (`put` is by id), no skipped removal (the cursor did not move), and no
 *  moment where a note the operator just sent is missing from the thread. */

/** Every thread this pane holds, **keyed by grant id**. A draft, a cursor and a pending write
 *  belong to one grant: no event names a thread other than its own. */
export interface NotesState {
  threads: Record<string, NoteThread>;
}

export const notesInit: NotesState = { threads: {} };
const emptyThread = (): NoteThread => ({
  status: "loading",
  notes: [],
  seq: 0,
  draft: "",
  held: null,
  pending: [],
  refusal: null,
});
export const threadOf = (state: NotesState, grantId: string | null): NoteThread =>
  (grantId && state.threads[grantId]) || emptyThread();

export type NotesEvent =
  | { type: "open"; grantId: string }
  /** The receiver's cursor read: changes after `seq`, or (`reset`) the thread again. `after` is
   *  the cursor the read was ASKED from: a page is applied only while the thread still stands
   *  at that cursor (`threadReduce`). */
  | { type: "page"; grantId: string; page: unknown; after: number }
  /** The owner's read of its own store: the whole thread, and the change number it stood at
   *  (`seq`). A thread older than the one held is dropped. */
  | { type: "thread"; grantId: string; notes: unknown; ended: boolean; seq?: unknown }
  | { type: "failed"; grantId: string; status: number }
  | { type: "draft"; grantId: string; text: string }
  | { type: "submit"; grantId: string; client_key: string; text: string; now: number }
  /** An add's answer. `seq`: the change number the owner gave the add (`note.add` answers it;
   *  this instance's own route does not). See THE ORDER RULE. */
  | { type: "confirmed"; grantId: string; client_key: string; note: unknown; seq?: unknown }
  | { type: "unconfirmed"; grantId: string; client_key: string }
  | { type: "refused"; grantId: string; client_key: string; text: string }
  /** The owner answered `not_found` to a note write: that note is not in the thread for this
   *  side any more (it was hidden or dropped). By its retry key (an add) or its id (an edit or
   *  a delete). */
  | { type: "missing"; grantId: string; client_key?: string; noteId?: string }
  /** The drawer closed: what only lives "until the drawer closes" goes. */
  | { type: "close"; grantId: string }
  /** The pane cleared or left: nothing of any thread stays. */
  | { type: "reset" };

const ordered = (notes: Note[]) =>
  [...notes].sort((a, b) => a.created_at - b.created_at || a.note_id.localeCompare(b.note_id));

function put(notes: Note[], note: Note): Note[] {
  const at = notes.findIndex((n) => n.note_id === note.note_id);
  if (at < 0) return ordered([...notes, note]);
  const next = [...notes];
  next[at] = note;
  return ordered(next);
}

/** A note of this side that arrived by reading settles a pending write of the same text: an
 *  unconfirmed submission that WAS saved must not stay beside the note it became. */
function settleByRead(thread: NoteThread, before: Note[]): NoteThread {
  if (thread.pending.length === 0) return thread;
  const known = new Set(before.map((n) => n.note_id));
  let pending = thread.pending;
  let draft = thread.draft;
  let held = thread.held;
  for (const note of thread.notes) {
    if (known.has(note.note_id) || !note.mine || note.deleted) continue;
    const hit = pending.find((p) => p.text === note.text && note.created_at >= p.at - 60);
    if (!hit) continue;
    pending = pending.filter((p) => p !== hit);
    if (held?.key === hit.client_key) held = null;
    if (draft === hit.text) draft = "";
  }
  return pending === thread.pending ? thread : { ...thread, pending, draft, held };
}

/** A read was applied: a row that only waited for one (`saved`) goes. */
const unsaved = (pending: PendingNote[]) =>
  pending.some((p) => p.state === "saved") ? pending.filter((p) => p.state !== "saved") : pending;

function threadReduce(thread: NoteThread, event: Exclude<NotesEvent, { type: "reset" }>): NoteThread {
  switch (event.type) {
    case "open":
      return thread;
    case "page": {
      // AN OLDER ANSWER NEVER OVERWRITES A NEWER ONE. A page says "what changed after the cursor
      // I was asked from". If the thread has moved since (another read landed first), this page
      // was cut from an older state: applying it could put back a note whose removal the newer
      // read delivered, with the cursor already past that removal, so it would never be
      // delivered again and a hidden or deleted note would stay on screen. Dropped whole, a
      // `reset` page too; the next read asks from the cursor the thread has. The drawer makes
      // one read at a time (`NotesDrawer`); this is what holds if a second reader ever appears.
      if (event.after !== thread.seq) return thread;
      const page = obj(event.page);
      const reset = page.reset === true;
      let notes = reset ? [] : thread.notes;
      for (const entry of Array.isArray(page.changes) ? page.changes : []) {
        const change = obj(entry);
        const id = str(change.note_id);
        if (change.removed === true) notes = notes.filter((n) => n.note_id !== id);
        else {
          const note = cleanNote(change.note);
          if (note && note.note_id === id) notes = put(notes, note);
        }
      }
      // A cursor that is not one keeps the one this thread had: the next read asks after it.
      const seq = Math.max(reset ? 0 : thread.seq, cursorOf(page.seq, 0));
      return settleByRead({ ...thread, status: "live", notes, seq, pending: unsaved(thread.pending) }, thread.notes);
    }
    case "thread": {
      // The owner's whole thread, by its change number: one cut from an older state is dropped.
      const seq = cursorOf(event.seq, thread.seq);
      if (seq < thread.seq) return thread;
      const notes = ordered(
        (Array.isArray(event.notes) ? event.notes : []).map(cleanNote).filter((n): n is Note => n !== null),
      );
      return settleByRead(
        { ...thread, status: event.ended ? "ended" : "live", notes, seq, pending: unsaved(thread.pending) },
        thread.notes,
      );
    }
    case "failed": {
      // 404 (receiver) / 409 `ended` (owner's write): the share ended. 503: not connected.
      // Either way the receiver shows NO note; the draft stays until the drawer closes.
      if (event.status === 404) return { ...thread, status: "gone", notes: [], seq: 0, pending: [] };
      if (event.status === 503) return { ...thread, status: "offline", notes: [], seq: 0 };
      return thread;
    }
    case "draft":
      return { ...thread, draft: event.text, refusal: null };
    case "submit": {
      const rest = thread.pending.filter((p) => p.client_key !== event.client_key);
      return {
        ...thread,
        refusal: null,
        held: { text: event.text, key: event.client_key },
        pending: [...rest, { client_key: event.client_key, text: event.text, state: "sending", at: event.now }],
      };
    }
    case "confirmed": {
      // THE ORDER RULE, door 3. The submission is settled either way; its CONTENT enters only
      // when the answer is ahead of what the thread has consumed.
      const note = cleanNote(event.note);
      const sent = thread.pending.find((p) => p.client_key === event.client_key);
      const at = cursorOf(event.seq, -1);
      const reading = thread.status === "live" || thread.status === "loading";
      const ahead = note !== null && reading && at > thread.seq;
      // No change number (this instance's own route): the row stays, marked saved, until the
      // next read that is applied brings the note (or shows it is not there).
      const waits = note !== null && reading && at < 0;
      return {
        ...thread,
        notes: ahead && note ? put(thread.notes, note) : thread.notes,
        pending: waits
          ? thread.pending.map((p) => (p.client_key === event.client_key ? { ...p, state: "saved" as const } : p))
          : thread.pending.filter((p) => p.client_key !== event.client_key),
        // The key is spent; the box clears only if it still holds exactly what was sent.
        held: thread.held?.key === event.client_key ? null : thread.held,
        draft: sent && thread.draft === sent.text ? "" : thread.draft,
      };
    }
    case "unconfirmed":
      return {
        ...thread,
        pending: thread.pending.map((p) =>
          p.client_key === event.client_key ? { ...p, state: "unconfirmed" as const } : p,
        ),
      };
    case "refused":
      // A confirmed failure: nothing was saved, the row goes, the draft and its key stay.
      return {
        ...thread,
        refusal: event.text,
        pending: thread.pending.filter((p) => p.client_key !== event.client_key),
      };
    case "missing": {
      // Not an error to retry and not a draft to send again: the note WAS saved and is gone for
      // this side. Its pending row, its key and (if the box still holds exactly it) its text go,
      // so no press can send it a second time as a new note; a shown note of that id goes too.
      const sent = thread.pending.find((p) => p.client_key === event.client_key);
      return {
        ...thread,
        refusal: NOTE_GONE,
        notes: event.noteId ? thread.notes.filter((n) => n.note_id !== event.noteId) : thread.notes,
        pending: thread.pending.filter((p) => p.client_key !== event.client_key),
        held: thread.held && thread.held.key === event.client_key ? null : thread.held,
        draft: sent && thread.draft === sent.text ? "" : thread.draft,
      };
    }
    case "close":
      return thread.status === "gone" || thread.status === "offline" || thread.status === "ended"
        ? { ...thread, draft: "", held: null, pending: [], refusal: null }
        : { ...thread, refusal: null };
  }
}

export function notesReduce(state: NotesState, event: NotesEvent): NotesState {
  if (event.type === "reset") return notesInit;
  const before = state.threads[event.grantId] ?? emptyThread();
  const after = threadReduce(before, event);
  return after === before && state.threads[event.grantId]
    ? state
    : { threads: { ...state.threads, [event.grantId]: after } };
}

/** What a failed note write was. Only an answer that says so is "nothing was saved". */
export type NoteFailure = "rate" | "ended" | "missing" | "offline" | "rejected" | "unconfirmed";
/** `side`: where the write was made.
 *
 *  On the RECEIVING side the write crosses the channel, so what is proven is the shared
 *  classifier's call (`writeFailure`, #2061): `504 peer_lost`, a timeout, `429 busy` (the hourly
 *  bound, or an owner on an older build that answered `busy` after it saved) and everything
 *  unknown are `unconfirmed`. A `404` says which of two things it is by its code: `not_found` is
 *  the NOTE (the owner hid it: a repeated add, an edit or a delete of it finds nothing),
 *  `no_grant` is the share.
 *
 *  The owner's own routes are this instance's store: no channel, so their answer is the outcome
 *  (`429` is the hourly bound, and a `404` is the share's: they do not tell the two apart). */
export function noteFailure(status: number, code: unknown, side: "owner" | "peer" = "owner"): NoteFailure {
  if (side === "peer") {
    if (status === 404 && code === "not_found") return "missing";
    const failure = writeFailure(status, code);
    if (failure === "refused") return "ended";
    if (failure === "offline") return "offline";
    return failure === "unconfirmed" ? "unconfirmed" : "rejected";
  }
  if (status === 429) return "rate";
  if (status === 404 || (status === 409 && code === "ended")) return "ended";
  if (status === 503) return "offline";
  if (status >= 400 && status < 500 && status !== 408) return "rejected";
  return "unconfirmed";
}

/** The words over one note's text. */
export function noteMarks(note: Note, peer: string): { who: string; you: boolean; marks: string[] } {
  const marks: string[] = [];
  if (note.deleted) marks.push("deleted");
  else if (note.edited_at !== null) marks.push("edited");
  if (note.hidden) marks.push(`hidden from ${peer}`);
  return { who: note.mine ? "You" : peer, you: note.mine, marks };
}

/** What may be done to a note. Own notes: edit and delete. The owner may hide any note of the
 *  peer's that is still shown. Nothing else exists: no control forwards, quotes or sends one. */
export function noteActions(note: Note, side: "owner" | "peer", writable: boolean): ("edit" | "delete" | "hide")[] {
  if (!writable || note.deleted) return [];
  if (note.mine) return ["edit", "delete"];
  return side === "owner" && !note.hidden ? ["hide"] : [];
}

// ---- Continue here ----------------------------------------------------------------------------

export const CONTINUE_LEAD = "Start your own session from this conversation.";
export const CONTINUE_WARNING_WORD = "From another instance";
export const continueWarning = (owner: string) =>
  `This text comes from ${owner}'s instance. Your agent is told to treat it as information, not instructions.`;
export const continueExplains = (owner: string) =>
  `It copies what you can read into a new session on this instance. ${owner}'s session is not changed, and nothing is sent to it.`;
export const CONTINUE_BUILDING = "Building a summary with your AI endpoint…";
export const CONTINUE_BUILDING_NOTE = "You will see it before anything is created.";
export const CONTINUE_PREVIEW_HEAD = "Check what your agent will get";
export const CONTINUE_PREVIEW_NOTE = "Nothing has been created yet. Start session creates it with exactly this text.";
export const CONTINUE_GONE = "This share is no longer available. Nothing was created.";
export const continueOffline = (owner: string) => `${owner} is not connected. Nothing was created.`;
export const CONTINUE_EXPIRED = "That preview expired. Nothing was created. Prepare it again.";
export const CONTINUE_UNCONFIRMED =
  "Could not confirm whether the session was created. Check your sessions before you start it again.";

/** What a failed CREATE was. The create route takes the handle (one use), reads the source once
 *  more and only then creates the local session, so:
 *
 *  - `409 gone` and `410 expired` are that route's own answers BEFORE it creates anything (the
 *    last re-read did not answer, whatever the reason, `peer_lost` included: that read has no
 *    effect on the owner; or the handle is spent or old). Proven: nothing was created.
 *  - everything else is the shared classifier's call (`writeFailure`, #2061). What it does not
 *    prove (no answer, a 5xx, any status or code nobody listed) is `unconfirmed`: this
 *    instance may have created the session and lost the answer on the way to the browser. */
export type ContinueCreateFailure = "gone" | "expired" | "offline" | "rejected" | "unconfirmed";
export function continueCreateFailure(status: number, code: unknown): ContinueCreateFailure {
  if (status === 409 && code === "gone") return "gone";
  if (status === 410 && code === "expired") return "expired";
  const failure = writeFailure(status, code);
  if (failure === "refused") return "gone";
  if (failure === "offline") return "offline";
  return failure === "unconfirmed" ? "unconfirmed" : "rejected";
}
/** A create of an API agent's session whose first turn was refused before the session took it
 *  (`delivery: null`): the session exists and the brief is still bound to it, fencing every other
 *  send. Try again commits the handoff's own handle (`retry_handle`) on the ordinary handoff
 *  route, which re-sends the SAME brief to the SAME session: it never makes a second one. */
export const CONTINUE_NOT_TAKEN =
  "The new session was created, but it did not take the brief yet. Try again sends the same brief to the same session. It never makes a second session.";
export const CONTINUE_RETRY_LOST =
  "Couldn't confirm that the session took the brief. Try again retries the same one. It never makes a second session.";
export const CONTINUE_RETRY_EXPIRED =
  "This brief expired before the new session took it. The session was created without it: continue there from your sessions, or prepare a new brief.";
export const CONTINUE_WITHHELD =
  "The brief now holds a known secret, so it was not sent. The new session was created without it.";

export const recordLabel = (owner: string) => `Record from ${owner} · not instructions`;
export const startedFromLine = (owner: string) => `Started from ${owner}'s shared session`;

export type ContinueMode = "quick" | "ai";
export interface ContinuePrepared {
  handle: string;
  preview: string;
  mode: ContinueMode;
  notice: string | null;
}

/** The dialog's stage. `choose` → `preparing` → `preview` → `creating` → (the dialog closes and
 *  the new session opens). **Only `preview` can lead to a creation**, and only by `create`.
 *  `ended` and `offline` are final for this dialog: nothing was created. `unconfirmed` is final
 *  too: a create got no usable answer, so a session MAY exist. The handle is spent either way
 *  (the same one again answers `410`), and a new preview would be a new handle and so a second
 *  session: the dialog offers neither, it says to check the sessions and closes. */
export type ContinueStage =
  | { stage: "choose"; error: string | null }
  | { stage: "preparing"; token: number; mode: ContinueMode }
  | { stage: "preview"; prepared: ContinuePrepared; error: string | null }
  | { stage: "creating"; token: number; prepared: ContinuePrepared }
  | { stage: "ended"; text: string }
  | { stage: "offline"; text: string }
  | { stage: "unconfirmed"; text: string }
  /** Created, the brief NOT taken. `retryHandle` null: nothing can send it any more (it expired,
   *  or was withheld), Close only. `retrying`: a Try again is in flight under `token`. */
  | {
      stage: "undelivered";
      made: ContinueMade;
      retryHandle: string | null;
      text: string;
      detail: string | null;
      retrying: boolean;
      token: number;
    };

/** The session a create made, as the dialog navigates to it. */
export interface ContinueMade {
  id: string;
  engine: string;
  native: string;
  cwd: string;
}

export interface ContinueState {
  view: ContinueStage;
  /** Bumped by every step that makes an answer in flight stale. */
  token: number;
}

export const continueInit: ContinueState = { view: { stage: "choose", error: null }, token: 0 };

export type ContinueEvent =
  | { type: "prepare"; mode: ContinueMode }
  | { type: "prepared"; token: number; answer: unknown; mode: ContinueMode }
  | { type: "failed"; token: number; status: number; code: unknown; message: string; owner: string }
  /** Cancel while preparing, or Back from the preview: nothing was created; back to the start. */
  | { type: "back" }
  | { type: "create" }
  /** The create (or a Try again) answered: the session exists and did not take the brief. */
  | { type: "undelivered"; token: number; made: ContinueMade; retryHandle: string | null; detail: string | null }
  /** The brief was withheld (it now holds a known secret): nothing can send it. */
  | { type: "withheld"; token: number; made: ContinueMade }
  | { type: "retry" }
  /** A Try again that failed: `final` when the handle can't be committed any more (expired). */
  | { type: "retry_failed"; token: number; text: string; final: boolean };

export function cleanPrepared(raw: unknown, mode: ContinueMode): ContinuePrepared | null {
  const o = obj(raw);
  const meta = obj(o.meta);
  if (typeof o.handle !== "string" || !o.handle || typeof o.preview !== "string") return null;
  return {
    handle: o.handle,
    preview: o.preview,
    mode: meta.mode === "ai" || meta.mode === "quick" ? meta.mode : mode,
    notice: typeof meta.notice === "string" ? meta.notice : null,
  };
}

export function continueReduce(state: ContinueState, event: ContinueEvent): ContinueState {
  const { view } = state;
  switch (event.type) {
    case "prepare":
      if (view.stage !== "choose") return state;
      return { token: state.token + 1, view: { stage: "preparing", token: state.token + 1, mode: event.mode } };
    case "prepared": {
      if (view.stage !== "preparing" || view.token !== event.token) return state; // cancelled meanwhile
      const prepared = cleanPrepared(event.answer, event.mode);
      return prepared
        ? { ...state, view: { stage: "preview", prepared, error: null } }
        : { ...state, view: { stage: "choose", error: "The brief could not be prepared. Nothing was created." } };
    }
    case "failed": {
      const flying = (view.stage === "preparing" || view.stage === "creating") && view.token === event.token;
      if (!flying) return state;
      if (view.stage === "creating") {
        const failure = continueCreateFailure(event.status, event.code);
        if (failure === "unconfirmed")
          return { ...state, view: { stage: "unconfirmed", text: CONTINUE_UNCONFIRMED } };
        if (failure === "gone") return { ...state, view: { stage: "ended", text: CONTINUE_GONE } };
        if (failure === "offline")
          return { ...state, view: { stage: "offline", text: continueOffline(event.owner) } };
        // A proven refusal. The handle is spent (the server removes it before it re-reads the
        // source) or expired, so the dialog returns to its first step: there is nothing left to
        // press Start session on.
        return {
          ...state,
          view: {
            stage: "choose",
            error:
              failure === "expired"
                ? CONTINUE_EXPIRED
                : `${event.message || "The session could not be created."} Nothing was created.`,
          },
        };
      }
      // Preparing creates nothing, whatever its answer was.
      if (event.status === 404 || event.code === "gone")
        return { ...state, view: { stage: "ended", text: CONTINUE_GONE } };
      if (event.status === 503 && event.code === "peer_offline")
        return { ...state, view: { stage: "offline", text: continueOffline(event.owner) } };
      if (event.status === 410) return { ...state, view: { stage: "choose", error: CONTINUE_EXPIRED } };
      return { ...state, view: { stage: "choose", error: event.message || "The brief could not be prepared." } };
    }
    case "back":
      if (view.stage === "preparing" || view.stage === "preview")
        return { token: state.token + 1, view: { stage: "choose", error: null } };
      return state;
    case "create":
      if (view.stage !== "preview") return state;
      return { token: state.token + 1, view: { stage: "creating", token: state.token + 1, prepared: view.prepared } };
    case "undelivered":
    case "withheld": {
      const flying =
        (view.stage === "creating" && view.token === event.token) ||
        (view.stage === "undelivered" && view.retrying && view.token === event.token);
      if (!flying) return state;
      const withheld = event.type === "withheld";
      return {
        ...state,
        view: {
          stage: "undelivered",
          made: event.made,
          retryHandle: withheld ? null : event.retryHandle,
          text: withheld ? CONTINUE_WITHHELD : event.retryHandle ? CONTINUE_NOT_TAKEN : CONTINUE_RETRY_EXPIRED,
          detail: withheld ? null : event.detail,
          retrying: false,
          token: event.token,
        },
      };
    }
    case "retry":
      if (view.stage !== "undelivered" || view.retrying || !view.retryHandle) return state;
      return { token: state.token + 1, view: { ...view, retrying: true, token: state.token + 1 } };
    case "retry_failed":
      if (view.stage !== "undelivered" || !view.retrying || view.token !== event.token) return state;
      return {
        ...state,
        view: {
          ...view,
          retrying: false,
          text: event.text,
          detail: null,
          retryHandle: event.final ? null : view.retryHandle,
        },
      };
  }
}

/** The seed, split for the preview: what stands before the record, the record's own lines, and
 *  what stands after. The record is found by the server's delimiters (`handoff.REMOTE_OPEN` /
 *  `REMOTE_CLOSE`); a seed without them is shown whole as the record, never as instructions.
 *  Every part is plain text. */
export function seedParts(seed: string): { before: string; record: string; after: string } {
  const lines = seed.split("\n");
  const open = lines.findIndex((l) => l.startsWith("<<<RECORD "));
  const close = open < 0 ? -1 : lines.findIndex((l, i) => i > open && l.startsWith("RECORD ") && l.endsWith(">>>"));
  if (open < 0 || close < 0) return { before: "", record: seed, after: "" };
  return {
    before: lines.slice(0, open).join("\n").trim(),
    record: lines.slice(open, close + 1).join("\n"),
    after: lines.slice(close + 1).join("\n").trim(),
  };
}

/** A session this tab just started with Continue here: which instance its first message came
 *  from. Tab memory only (the server writes no link back, by design): a reload forgets it. */
const STARTED_MAX = 16;
const started = new Map<string, string>();
export const startedFrom = {
  set(sessionKey: string, owner: string): void {
    started.delete(sessionKey);
    started.set(sessionKey, owner);
    while (started.size > STARTED_MAX) {
      const oldest = started.keys().next().value;
      if (oldest === undefined) break;
      started.delete(oldest);
    }
  },
  get: (sessionKey: string): string | null => started.get(sessionKey) ?? null,
  clear: (): void => started.clear(),
};

// ---- the remote pane's menu -------------------------------------------------------------------

export type RemoteMenuItem = "continue" | "notes" | "ask";

/** What the remote pane's ⋯ menu offers, each only when it applies. It has no session action:
 *  nothing in it archives, renames, tags or moves anything. */
export function remoteMenu(input: { live: boolean; level: string | null; pendingAsk: boolean }): RemoteMenuItem[] {
  if (!input.live || !input.level) return [];
  const items: RemoteMenuItem[] = ["continue", "notes"];
  if (askLevels(input.level).length > 0 && !input.pendingAsk) items.push("ask");
  return items;
}

export { remoteAccessWords };
