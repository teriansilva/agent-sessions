/** Federation (#1246 P2, #2052): the shapes `GET /api/federation` answers with, and the pure
 *  mapping from them to what Settings → Peers shows. No React here, so every state the page can be
 *  in is a function of plain data and is unit-tested (`federation.test.ts`).
 *
 *  The server contract is docs/invariants/federation.md. */
import type { PublicTurn } from "../share/publicApi";
import { ago } from "./shareFormat";

/** How an instance can be reached. The read carries the KIND only, never an address. */
export type ReachKind = "direct" | "relay" | (string & {});

export interface FederationPeer {
  peer_id: string;
  name: string;
  state: "trusted" | "revoked";
  /** `null` in a process that does not own federation: it cannot know. */
  connected: boolean | null;
  /** The other side told this instance it no longer trusts it. */
  refused: boolean;
  reachable_by: ReachKind[];
  created_at: number;
  last_seen_at: number | null;
  revision: number;
}

export type PairingState = "dialing" | "confirm" | "done" | "denied" | "failed" | "expired";

export interface FederationPairing {
  id: string;
  role: "inviter" | "acceptor";
  state: PairingState;
  /** Six digits, only while `state` is `confirm`. */
  code: string;
  peer_id: string;
  /** The name the other instance suggests for itself: cleaned server-side, rendered as text. */
  name: string;
  local_confirmed: boolean;
  remote_confirmed: boolean;
  expires_at: number;
  error: string;
}

export interface FederationInviteRef {
  id: string;
  expires_at: number;
}

export interface FederationState {
  /** Whether THIS process serves federation for the store. */
  owner: boolean;
  self: { peer_id: string; name: string; reachable_by: ReachKind[] };
  peers: FederationPeer[];
  invites: FederationInviteRef[];
  pairings: FederationPairing[];
}

/** `POST /api/federation/invites`: the one answer that carries the invite string. */
export interface CreatedInvite extends FederationInviteRef {
  invite: string;
}

// ---- formatting -------------------------------------------------------------------------------

/** A peer id in groups of four: `k3wq7n2b…` → `["k3wq", "7n2b", …]`. */
export function groupPeerId(peerId: string): string[] {
  return peerId.match(/.{1,4}/g) ?? [];
}

/** The pairing code in two groups of three: `482915` → `["482", "915"]`. */
export function groupCode(code: string): string[] {
  return code.match(/.{1,3}/g) ?? [];
}

/** `m:ss` left until `expiresAtS`, never negative: `581.2` s → `9:41`. */
export function formatCountdown(expiresAtS: number, nowS: number): string {
  const left = Math.max(0, Math.ceil(expiresAtS - nowS));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

/** "directly", "through the relay", both, or `null` when nothing can reach this instance. */
export function reachableLabel(kinds: readonly ReachKind[]): string | null {
  const words = [...new Set(kinds)].map((k) =>
    k === "direct" ? "directly" : k === "relay" ? "through the relay" : k,
  );
  return words.length ? words.join(" and ") : null;
}

/** A server sentence fragment as a sentence: "the pairing timed out" → "The pairing timed out." */
export function sentence(text: string): string {
  const t = text.trim();
  if (!t) return "";
  const s = t[0].toUpperCase() + t.slice(1);
  return /[.!?…]$/.test(s) ? s : `${s}.`;
}

// ---- peer rows --------------------------------------------------------------------------------

export type PeerRowKind = "connected" | "offline" | "unknown" | "refused" | "revoked";

export interface PeerRowState {
  kind: PeerRowKind;
  /** LED tone: green only for connected, amber for "needs you", grey for everything quiet. */
  led: "up" | "att" | "idle";
  /** The word beside the LED. */
  word: string;
  canRename: boolean;
  /** The label of the action that ends trust, or `null` when there is none (already revoked). */
  revokeLabel: "Revoke" | "Remove" | null;
}

/** What one peer row shows. `owner` is the read's `owner`: a process that does not own federation
 *  cannot know whether a peer is connected, so it never claims "connected" or "offline". */
export function peerRowState(
  peer: Pick<FederationPeer, "state" | "connected" | "refused" | "last_seen_at">,
  owner: boolean,
  nowS: number,
): PeerRowState {
  if (peer.state === "revoked")
    return { kind: "revoked", led: "idle", word: "revoked", canRename: false, revokeLabel: null };
  if (!owner || peer.connected == null)
    return {
      kind: "unknown",
      led: "idle",
      word: "connection not shown here",
      canRename: false,
      revokeLabel: "Revoke",
    };
  if (peer.refused)
    return {
      kind: "refused",
      led: "att",
      word: "removed by the other side",
      canRename: false,
      revokeLabel: "Remove",
    };
  if (peer.connected)
    return { kind: "connected", led: "up", word: "connected", canRename: true, revokeLabel: "Revoke" };
  const seen =
    peer.last_seen_at == null ? "not seen yet" : `last seen ${ago(peer.last_seen_at, nowS)}`;
  return {
    kind: "offline",
    led: "idle",
    word: `offline · ${seen}`,
    canRename: true,
    revokeLabel: "Revoke",
  };
}

// ---- the pairing card -------------------------------------------------------------------------

/** A pairing the page is following: the server's last word on it, or `gone` once an OPEN pairing
 *  is missing from a read. Missing does NOT say how it ended: the server restarted (nothing
 *  stored), or the pairing completed while this page was not reading and its record was swept
 *  (the peer IS stored). So a gone pairing carries what the SAME read's peer list says about its
 *  instance: `stored` true (trusted there), false (not there), or undefined (no identity to look
 *  up). The page never claims an outcome the durable list does not show. */
export type TrackedPairing = FederationPairing & { gone?: boolean; stored?: boolean };

export type PairingCardKind =
  | "dialing"
  | "confirm"
  | "waiting"
  | "done"
  | "denied"
  | "failed"
  | "expired"
  | "gone";

/** The three endings of a pairing the server stopped listing while it was open. */
export const PAIRING_GONE_STORED =
  "This pairing is no longer open. The instance is in your list of peers.";
export const PAIRING_GONE_NOT_STORED =
  "This pairing is no longer open. The instance is not in your list of peers.";
export const PAIRING_GONE_UNKNOWN =
  "This pairing is no longer open. Check your list of peers to see whether it completed.";
/** An invite the server stopped listing, or whose time ran out: the page does not know which
 *  instance held it, so it cannot look the outcome up and does not guess one. */
export const INVITE_GONE =
  "This invite is no longer open. If the other instance used it, check your list of peers.";
export const INVITE_EXPIRED =
  "This invite expired. If the other instance used it before that, check your list of peers.";

export function goneMessage(p: Pick<TrackedPairing, "stored">): string {
  if (p.stored === true) return PAIRING_GONE_STORED;
  return p.stored === false ? PAIRING_GONE_NOT_STORED : PAIRING_GONE_UNKNOWN;
}

export function isOpenPairing(p: Pick<FederationPairing, "state">): boolean {
  return p.state === "dialing" || p.state === "confirm";
}

/** Which card a followed pairing is drawn as. Driven by the server's state alone. */
export function pairingCardKind(p: TrackedPairing): PairingCardKind {
  if (p.gone) return "gone";
  if (p.state === "confirm") return p.local_confirmed ? "waiting" : "confirm";
  return p.state;
}

/** The settled message: the server's own reason, then what it means for the store. */
export function settledMessage(p: TrackedPairing): string {
  const kind = pairingCardKind(p);
  if (kind === "gone") return goneMessage(p);
  const fallback =
    kind === "expired"
      ? "The pairing timed out."
      : kind === "denied"
        ? "The pairing was cancelled."
        : "The pairing did not complete.";
  return `${sentence(p.error) || fallback} Nothing was stored.`;
}

/** Fold one read into the pairings the page follows.
 *
 *  A pairing is followed from the moment it is seen OPEN (or was started here, see
 *  `PeersSettings`), and keeps being followed through its settled state, so a failure stays on
 *  screen with its reason after the server stops listing it. A followed pairing that was open and
 *  is missing from the read is `gone`, with `stored` taken from THIS read's peers (and kept up
 *  to date on later reads). A settled one that is missing just keeps its last state. */
export function trackPairings(
  tracked: readonly TrackedPairing[],
  listed: readonly FederationPairing[],
  peers: readonly Pick<FederationPeer, "peer_id" | "state">[],
): TrackedPairing[] {
  const byId = new Map(listed.map((p) => [p.id, p]));
  const trusted = new Set(peers.filter((p) => p.state === "trusted").map((p) => p.peer_id));
  const out: TrackedPairing[] = tracked.map((t) => {
    const now = byId.get(t.id);
    if (now) return now;
    if (!isOpenPairing(t) && !t.gone) return t;
    const stored = t.peer_id ? trusted.has(t.peer_id) : undefined;
    return t.gone && t.stored === stored ? t : { ...t, gone: true, stored };
  });
  const known = new Set(tracked.map((t) => t.id));
  for (const p of listed) if (!known.has(p.id) && isOpenPairing(p)) out.push(p);
  return out;
}

// ---- the invite card --------------------------------------------------------------------------

export type InviteCardKind = "open" | "taken" | "expired" | "gone";

/** What became of the invite this page created.
 *
 *  - `open`: the server still lists it and its time is not up.
 *  - `taken`: the other instance used it. Its pairing carries the invite's id (the inviter side).
 *  - `expired`: its ten minutes are over.
 *  - `gone`: the server no longer lists it and nothing took it (a restart). */
export function inviteCardKind(
  invite: FederationInviteRef,
  state: Pick<FederationState, "invites" | "pairings">,
  nowS: number,
): InviteCardKind {
  if (state.pairings.some((p) => p.id === invite.id)) return "taken";
  if (nowS >= invite.expires_at) return "expired";
  return state.invites.some((i) => i.id === invite.id) ? "open" : "gone";
}

/** How often the page re-reads: fast while the other operator's action can change something. */
export const POLL_ACTIVE_MS = 2_000;
export const POLL_IDLE_MS = 15_000;

export function pollInterval(inviteOpen: boolean, tracked: readonly TrackedPairing[]): number {
  return inviteOpen || tracked.some((p) => isOpenPairing(p) && !p.gone)
    ? POLL_ACTIVE_MS
    : POLL_IDLE_MS;
}

// ---- sharing a session with a peer (#2054, #2055) ---------------------------------------------

/** A grant as the operator API shows it (`grants.public` plus the peer's name). */
export interface FederationGrant {
  grant_id: string;
  session_key: string;
  peer_id: string;
  peer_name: string;
  /** What the grant shares (#2056): an API session's conversation, or a terminal's screen. An
   *  answer without it is from before kinds: an API grant. */
  kind?: string;
  level: string;
  expires_at: number;
  revision: number;
  state: string;
  created_at: number;
  ended_at: number | null;
  end_reason: string | null;
  /** The session's title: only on the per-peer list (`?peer=`), which has no pane around it. */
  title?: string;
  /** The peer read this session within the last 30 s (#2059): one bit, nothing more. */
  watching?: boolean;
  /** How many notes the thread with this peer shows (#2062). A number. */
  notes?: number;
}

export interface FederationGrantList {
  grants: FederationGrant[];
  generation: number;
  levels: string[];
  default_level?: string;
  default_expires_in: Record<string, number>;
  max_expires_in: number;
  max_expires_in_by_level?: Record<string, number>;
  send_risk?: string;
}

/** One session a peer shares with this instance: everything `GET /api/federation/remote` knows
 *  of it. There is no summary, no activity time and no working state, so no row shows one. */
export interface RemoteShare {
  key: string;
  peer_id: string;
  peer_name: string;
  connected: boolean;
  grant_id: string;
  engine: string;
  native_id: string;
  title: string;
  /** `api` or `terminal` (#2056). Absent on an answer from before kinds: an API session. */
  kind?: string;
  level: string;
  expires_at: number;
  revision: number;
  seen_at: number;
}

/** What a share is, for everything that draws it differently: a conversation, or a screen. */
export type ShareKind = "api" | "terminal";
/** The kind of a listed share. Only the server's own word makes one a terminal. */
export const shareKind = (r: { kind?: string }): ShareKind =>
  r.kind === "terminal" ? "terminal" : "api";

/** The expiry choices of the dialog, in days. Seven is the server's default too. */
export const GRANT_EXPIRY_DAYS = [1, 7, 30, 90] as const;
export type GrantExpiryDays = (typeof GRANT_EXPIRY_DAYS)[number];
export const GRANT_EXPIRY_DEFAULT: GrantExpiryDays = 7;
export const expiresInSeconds = (days: number) => days * 86400;

/** What the dialog says, always, about what the other instance will see (#2055). */
export const PEER_DISCLOSURE =
  "The other instance sees this conversation as you see it here, without your secrets we know of." +
  " A secret we don't know about is not hidden.";
export const PEER_DISCLOSURE_DETAIL =
  "It gets the turns and the tool activity. Never the folder, the model settings or files; an" +
  " image shows as its name only.";

/** What the dialog says FIRST about a terminal (#2060): the limit of what is hidden, before the
 *  operator picks anything. The second sentence is the unprotected case and is set in bold.
 *
 *  It says exactly what `docs/invariants/federation.md` ("The stated limits") says, in plain
 *  words, and nothing more: masked is a complete known secret on one line or wrapped at the
 *  screen's edge; not masked is a value cut off, a value a program lays out across lines itself,
 *  and a secret BattleLab does not know; not shown at all is a screen after a control the renderer
 *  does not model, and a screen with any character outside `vtscreen._one_cell`'s allowlist. A
 *  change that claims more must change that paragraph in the same commit. */
export const TERMINAL_LIMIT_WORD = "Know the limit";
export const TERMINAL_LIMIT_LEAD =
  "The peer sees this terminal's screen. A secret we know of is hidden when all of it is on one" +
  " line or it wraps at the screen's edge.";
export const TERMINAL_LIMIT_UNPROTECTED =
  "A secret that is cut off, one a program lays out across lines itself, or one we don't know" +
  " about is not hidden.";
/** When the peer gets no screen at all: what the renderer refuses rather than risk a misplaced
 *  mask. Shared by the dialog and the screen pane's "can't be shown" state. */
export const TERMINAL_NOT_SHOWN =
  "No screen is sent after a control we can't redraw exactly, such as an edit in the middle of a" +
  " shell line, or while the screen shows any character outside plain Latin, Greek and Cyrillic text," +
  " punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic, Indic and similar" +
  " scripts, or combining characters. An agent that draws emoji is often not viewable.";
export const TERMINAL_LIMIT_DETAIL =
  "It is a picture of the screen as text. The peer cannot type, resize or scroll back.";
export const TERMINAL_VIEW_ONLY = "A terminal can only be watched.";

const dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
/** The latest instant a JS `Date` can hold, in seconds (±8.64e15 ms). */
export const EPOCH_S_MAX = 8.64e12;
/** What a date that cannot be one reads as. Neutral: it says nothing about the share. */
export const UNKNOWN_DATE = "an unknown date";

/** Whether `v` is an epoch in seconds this browser can turn into a date: a finite number after 0
 *  and within `Date`'s range. A number a peer listed is untrusted (#2055 review): `1e300` is
 *  finite, and `new Date(1e303)` is an Invalid Date that `Intl.DateTimeFormat` THROWS on. */
export function isEpochS(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v > 0 && v <= EPOCH_S_MAX;
}

/** "22 Nov" in the browser's time zone. The ONE place an expiry becomes a `Date`: every surface
 *  that shows one (the list row, the map node, the pane's banner, Settings → Peers, the share
 *  dialog) goes through here, and it never throws. A value that is not a date — already stored by
 *  an earlier build, or from anywhere else — reads as {@link UNKNOWN_DATE}. */
export function shortDate(epochS: unknown): string {
  if (!isEpochS(epochS)) return UNKNOWN_DATE;
  try {
    return dayMonth.format(new Date(epochS * 1000));
  } catch {
    return UNKNOWN_DATE;
  }
}

const hourMinute = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
/** What a time that cannot be one reads as (#2061). */
export const UNKNOWN_TIME = "an unknown time";

/** "14:05" in the browser's time zone: the time a peer's answer carries (a suggestion's
 *  `created_at` in "Your suggestions", #2061). With {@link shortDate}, the only places a peer's
 *  number becomes a `Date`; the same check, and it never throws. Anything that is not a date
 *  (`1e300`, `NaN`, a negative, a string) reads as {@link UNKNOWN_TIME}. */
export function shortTime(epochS: unknown): string {
  if (!isEpochS(epochS)) return UNKNOWN_TIME;
  try {
    return hourMinute.format(new Date(epochS * 1000));
  } catch {
    return UNKNOWN_TIME;
  }
}

/** "View" for `view`; a level this build does not know is shown as the server named it. */
export function levelLabel(level: string): string {
  return level ? level[0].toUpperCase() + level.slice(1) : "";
}

export interface GrantRowState {
  /** Who (in a session's dialog) or what (in a peer's list) the row is about. */
  title: string;
  level: string;
  /** "until 22 Nov", or a countdown ("ends in 42 min") for a grant ending within a day. */
  until: string;
  /** Whether `until` is a countdown: it is then re-read as the clock moves. */
  countdown: boolean;
}

const HOUR = 3600;
/** A grant ending within this long shows a countdown instead of a date (#2061). */
export const COUNTDOWN_WITHIN_S = 86400;

/** "ends in 42 min" / "ends in 3 h 5 min" / "ends in under a minute", from the SERVER's
 *  `expires_at` and this clock. Past the end it says "ended": the server decides, this only
 *  tells the operator what to expect. */
export function endsIn(expiresAt: number, now: number): string {
  const left = Math.floor(expiresAt - now);
  if (left <= 0) return "ended";
  if (left < 60) return "ends in under a minute";
  if (left < HOUR) return `ends in ${Math.floor(left / 60)} min`;
  const h = Math.floor(left / HOUR);
  const m = Math.floor((left % HOUR) / 60);
  return m ? `ends in ${h} h ${m} min` : `ends in ${h} h`;
}

/** When a grant ends, as a row says it: a countdown inside the last day, else the date. An
 *  expiry that is not a date (`isEpochS`: `NaN`, `1e300`, `0`, a negative, not a number) is
 *  neither: it reads "until an unknown date", never "ended" and never a countdown, and nothing
 *  here throws. */
export function grantEnds(expiresAt: unknown, now: number): { text: string; countdown: boolean } {
  if (!isEpochS(expiresAt) || !Number.isFinite(now))
    return { text: `until ${UNKNOWN_DATE}`, countdown: false };
  return expiresAt - now < COUNTDOWN_WITHIN_S
    ? { text: endsIn(expiresAt, now), countdown: true }
    : { text: `until ${shortDate(expiresAt)}`, countdown: false };
}

/** One row of "Shared with": in a session's dialog it names the peer, in a peer's list the
 *  session. A session without a readable title is shown by its key's short tail, never blank. */
export function grantRowState(
  grant: FederationGrant,
  by: "peer" | "session",
  now: number = Date.now() / 1000,
): GrantRowState {
  const title =
    by === "peer"
      ? grant.peer_name || grant.peer_id.slice(0, 8)
      : grant.title || `Session ${grant.session_key.split(":").pop()?.slice(0, 8) ?? ""}`.trim();
  const ends = grantEnds(grant.expires_at, now);
  return { title, level: levelLabel(grant.level), until: ends.text, countdown: ends.countdown };
}

/** The live grant of this session that already names `peerId`, if any: sharing again with the
 *  same peer changes that grant's expiry (with its revision) and never makes a second one. */
export function grantFor(
  grants: readonly FederationGrant[],
  peerId: string,
): FederationGrant | undefined {
  return grants.find((g) => g.peer_id === peerId);
}

/** The peers a session can be shared with: trusted ones. A revoked peer is not listed. */
export function shareablePeers(peers: readonly FederationPeer[]): FederationPeer[] {
  return peers.filter((p) => p.state === "trusted");
}

export interface RemoteRowState {
  title: string;
  peer: string;
  offline: boolean;
  /** A terminal's screen (#2060): the row and the node carry the SCREEN tag. */
  screen: boolean;
  /** "view only · until 22 Nov" (a screen: "until 22 Nov", beside its tag), or "offline" while
   *  the peer's channel is down. */
  meta: string;
  /** "until 22 Nov": the expiry alone, for a surface that shows the access elsewhere. */
  until: string;
  /** For assistive tech: the whole row in one sentence. */
  label: string;
}

/** What the mirrored level lets this instance do, in the banner's and the row's words. A level
 *  this build does not know is shown as the server named it. */
export function remoteAccessWords(level: string): string {
  if (level === "view") return "view only";
  if (level === "suggest") return "can suggest";
  if (level === "send") return "can send";
  return levelLabel(level).toLowerCase();
}

/** What a remote row (list or map) shows: exactly what the list read carries. */
export function remoteRowState(r: RemoteShare): RemoteRowState {
  const title = r.title || "(untitled)";
  const peer = r.peer_name || r.peer_id.slice(0, 8);
  const screen = shareKind(r) === "terminal";
  // A terminal share is a screen to watch (view only for good); an API share says its level.
  const access = screen ? "screen only" : remoteAccessWords(r.level);
  const offline = !r.connected;
  const until = `until ${shortDate(r.expires_at)}`;
  return {
    title,
    peer,
    offline,
    screen,
    // A screen's access is said by its tag, so the words beside it are the end date alone.
    meta: offline ? "offline" : screen ? until : `${access} · ${until}`,
    until,
    label: `Open ${title}, shared by ${peer}, ${offline ? `${screen ? "screen, " : ""}offline` : access}`,
  };
}

/** The Shared tab's search (#2198): the rows whose title or peer name, as the row shows them,
 *  contains the text. Case-insensitive, in the browser: the list read is the whole list, and no
 *  filter of the local session list is ever applied to these rows. */
export function searchRemoteShares(
  rows: readonly RemoteShare[],
  q: string,
): readonly RemoteShare[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((r) => {
    const row = remoteRowState(r);
    return (
      row.title.toLowerCase().includes(needle) || row.peer.toLowerCase().includes(needle)
    );
  });
}

/** The read-only pane's route. Its own route: never the local session route. */
export const REMOTE_ROUTE = "/remote/:peerId/:grantId";
export const remotePath = (peerId: string, grantId: string) =>
  `/remote/${encodeURIComponent(peerId)}/${encodeURIComponent(grantId)}`;

/** The shapes the server validates before it mirrors an entry (`identity.PEER_ID_RE`,
 *  `grants.GRANT_ID_RE`). A route whose ids are not these asks the server nothing. */
export const PEER_ID_RE = /^[a-z2-7]{32}$/;
export const GRANT_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;

/** How long an events read is held, in seconds. Through Home Free a proxied read is cut at 5 s,
 *  so the hold stays under it everywhere. */
export const REMOTE_EVENTS_WAIT_S = 3;
/** How often the pane asks again while the peer is offline or a read failed. */
export const REMOTE_RETRY_MS = 3_000;
/** A read that fails for a reason that says nothing about the grant (a timeout, a busy peer) is
 *  retried with the turns kept; after this many in a row the pane shows nothing. */
export const REMOTE_STALL_LIMIT = 3;

/** A wait that ends early, without throwing, when the read it belongs to is aborted. */
export function abortablePause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || ms <= 0) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done);
  });
}

/** A turn as the projection carries it. Structural, so this module needs nothing from the
 *  viewer; the pane hands the same objects to the viewer's turn component. */
export interface RemoteTurn {
  turn_id: string;
  [field: string]: unknown;
}

export interface RemoteSnapshot {
  link: string;
  revision: number | null;
  event_cursor: number | null;
  state?: string;
  active_turn?: string | null;
  turns: RemoteTurn[];
  omitted_turns: number;
  pending_requests: { request_id: string; summary?: string }[];
  background?: boolean;
  can_send: false;
  /** One bit (#2059): the owner's operator has this session's pane open. */
  owner_present?: boolean;
  /** Who settled the newest requests (#2059). Untrusted: read through `cleanDecisions`. */
  decisions?: unknown;
}

export interface RemoteEventPage {
  link: string;
  revision: number | null;
  next_cursor: number | null;
  events: { cursor: number; kind: string }[];
}

export interface RemoteHistoryPage {
  link: string;
  turns: RemoteTurn[];
  before: string | null;
  has_more: boolean;
}

/** `loading`: nothing read yet. `live`: showing what the owner answered. `offline`: the owner is
 *  not connected (or cannot be read): nothing is shown. `gone`: the share ended; final. */
export type RemotePaneStatus = "loading" | "live" | "offline" | "gone";

export interface RemotePaneState {
  /** `<peerId>/<grantId>` of the route this state is for. */
  target: string;
  /** THE FENCE. Every read carries the generation it started under; an answer under any other
   *  generation is discarded. Bumped whenever the pane clears: 404, 503, a new target, leaving. */
  generation: number;
  status: RemotePaneStatus;
  /** Milliseconds to wait before this generation's first read; `null` = read nothing (gone). */
  startAfterMs: number | null;
  snapshot: RemoteSnapshot | null;
  /** Older turns paged in with `history`, oldest first; they end where the snapshot begins. */
  older: RemoteTurn[];
  /** Whether turns older than everything held exist. */
  hasMore: boolean;
  /** Reads in a row that failed without saying anything about the grant. */
  stalls: number;
  /** This instance's own suggestions under the grant, as the owner lists them (ids and states,
   *  never text); `null` until the first read of this generation. */
  suggestions: OwnSuggestion[] | null;
  /** Messages this pane sent under this generation, oldest first, with what the owner answered. */
  outbox: OutboxRow[];
  /** The owner answered `held`: a mission runs that session, so nothing can be written now. */
  held: boolean;
  /** The mirrored revision under which the owner refused a write (`404`): this side's access
   *  to write ended, or the mirror is behind. Offering it again waits for a newer revision. */
  refusedRevision: number | null;
  /** Requests that were in flight when the page was reloaded (#2061): keys only, no content. */
  leftovers: Leftover[];
}

/** One of this instance's suggestions as `GET …/suggestions` lists it. */
export interface OwnSuggestion {
  suggestion_id: string;
  client_key: string;
  state: string;
  handoff: string | null;
  created_at: number;
  updated_at: number;
}

/** A message sent from this pane. `text` is in memory only (`null` for one found again after a
 *  reload, whose text was never stored). `delivery` is the owner's receipt as answered:
 *  `sending` until an answer, then `sent`, `queued`, `not_sent`, or `unconfirmed` (the receipt
 *  was `uncertain` or absent, or the answer never came). */
export interface OutboxRow {
  client_key: string;
  text: string | null;
  delivery: "sending" | "sent" | "queued" | "not_sent" | "unconfirmed";
  /** Our own turns that were already in the conversation when this was sent. */
  prior: string[];
  /** The conversation turn this message turned out to be, once the owner shows it. */
  turn: string | null;
}

/** What the content-free `sessionStorage` record of an in-flight request holds (#2061). */
export interface PendingOp {
  peer_id: string;
  grant_id: string;
  client_key: string;
  kind: "send" | "suggest";
  started_at: number;
}

/** A request the page was reloaded under, being found out about. `pending`: not asked yet (or
 *  asked under a generation that ended). `unknown`: the owner has no such key NOW, which is not
 *  "it did not arrive". `failed`: the status read itself failed. `final`: two minutes of
 *  unknown; the notice stays, nothing more is asked. */
export interface Leftover {
  client_key: string;
  kind: "send" | "suggest";
  started_at: number;
  phase: "pending" | "checking" | "unknown" | "failed" | "final";
  /** When the owner first answered unknown (ms); the schedule and the two minutes count from it. */
  firstUnknownAt: number | null;
  /** When to ask again (ms), or `null`: nothing scheduled. */
  nextAt: number | null;
}

/** What `GET …/operations/{client_key}` answers. */
export type OperationStatus =
  | { known: false }
  | { known: true; kind: "send"; handoff: string | null }
  | { known: true; kind: "suggest"; suggestion_id: string; state: string; handoff: string | null };

/** After the first unknown answer the pane asks again at these offsets (ms), and whenever the
 *  conversation changes; then it stops asking. */
export const RECONCILE_RECHECK_MS = [5_000, 30_000] as const;
export const RECONCILE_GIVE_UP_MS = 120_000;
/** How often the pane reads its own suggestions while one is waiting or being delivered. */
export const SUGGESTION_POLL_OWN_MS = 3_000;
/** A failed status read is retried this often while the peer can be reached. */
export const RECONCILE_RETRY_MS = 5_000;

export type RemotePaneEvent =
  | { type: "open"; target: string; leftovers?: PendingOp[] }
  | { type: "snapshot"; generation: number; snapshot: RemoteSnapshot }
  | { type: "history"; generation: number; page: RemoteHistoryPage }
  | { type: "failed"; generation: number; status: number }
  // Everything below is the answer to a request this pane made (#2061). Each carries the
  // generation it was made under, exactly like a read, and is dropped under any other.
  | { type: "suggestions"; generation: number; rows: unknown }
  | { type: "suggested"; generation: number; client_key: string; suggestion: unknown; now: number }
  | { type: "withdrawn"; generation: number; suggestion: unknown; now: number }
  | { type: "sending"; generation: number; client_key: string; text: string }
  | { type: "sent"; generation: number; client_key: string; handoff: unknown }
  | { type: "unconfirmed"; generation: number; client_key: string }
  | { type: "unsent"; generation: number; client_key: string }
  | { type: "held"; generation: number; held: boolean }
  | { type: "refused"; generation: number; revision: number | null }
  | { type: "checking"; generation: number; client_key: string }
  | {
      type: "status";
      generation: number;
      client_key: string;
      result: OperationStatus | "failed";
      now: number;
    }
  | { type: "giveup"; generation: number; client_key: string };

const freshLeftovers = (ops: readonly PendingOp[] | undefined): Leftover[] =>
  (ops ?? []).map((op) => ({
    client_key: op.client_key,
    kind: op.kind,
    started_at: op.started_at,
    phase: "pending",
    firstUnknownAt: null,
    nextAt: null,
  }));

export function remotePaneInit(target: string, leftovers?: PendingOp[]): RemotePaneState {
  return {
    target,
    generation: 0,
    status: "loading",
    startAfterMs: 0,
    snapshot: null,
    older: [],
    hasMore: false,
    stalls: 0,
    suggestions: null,
    outbox: [],
    held: false,
    refusedRevision: null,
    leftovers: freshLeftovers(leftovers),
  };
}

function cleared(
  state: RemotePaneState,
  status: RemotePaneStatus,
  startAfterMs: number | null,
  target = state.target,
  leftovers?: PendingOp[],
): RemotePaneState {
  return {
    target,
    generation: state.generation + 1,
    status,
    startAfterMs,
    snapshot: null,
    older: [],
    hasMore: false,
    stalls: 0,
    // Nothing the owner answered survives a clear: not the suggestion list, not a delivery
    // state, not `held`. A late answer for any of them is under the old generation.
    suggestions: null,
    outbox: [],
    held: false,
    refusedRevision: null,
    // The leftover KEYS are this browser's own and hold no content: on the same target they
    // stay (a check in flight is asked again under the new generation); another target brings
    // its own.
    leftovers:
      target === state.target
        ? state.leftovers.map((l) =>
            l.phase === "checking" || l.phase === "failed" ? { ...l, phase: "pending" } : l,
          )
        : freshLeftovers(leftovers),
  };
}

const SUGGESTION_STATES = new Set([
  "pending",
  "approving",
  "approved",
  "rejected",
  "withdrawn",
  "expired",
]);
const HANDOFFS = new Set(["sent", "queued", "not_sent", "uncertain"]);
const finite = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

/** The suggestions of an answer that can be shown: known states, string ids, each id once. */
function listedSuggestions(raw: unknown): OwnSuggestion[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: OwnSuggestion[] = [];
  for (const r of raw as Record<string, unknown>[]) {
    if (!r || typeof r !== "object") continue;
    const id = r.suggestion_id;
    if (typeof id !== "string" || !GRANT_ID_RE.test(id) || seen.has(id)) continue;
    if (typeof r.state !== "string" || !SUGGESTION_STATES.has(r.state)) continue;
    seen.add(id);
    out.push({
      suggestion_id: id,
      client_key: typeof r.client_key === "string" ? r.client_key : "",
      state: r.state,
      handoff: typeof r.handoff === "string" && HANDOFFS.has(r.handoff) ? r.handoff : null,
      created_at: finite(r.created_at),
      updated_at: finite(r.updated_at),
    });
  }
  return out;
}

/** `{suggestion_id, state}` of a suggest or withdraw answer, or `null` when it is not one. */
function suggestionRef(raw: unknown): { suggestion_id: string; state: string } | null {
  const r = raw as { suggestion_id?: unknown; state?: unknown } | null;
  if (!r || typeof r !== "object") return null;
  if (typeof r.suggestion_id !== "string" || !GRANT_ID_RE.test(r.suggestion_id)) return null;
  if (typeof r.state !== "string" || !SUGGESTION_STATES.has(r.state)) return null;
  return { suggestion_id: r.suggestion_id, state: r.state };
}

/** A receipt as the outbox shows it. Only `sent`, `queued` and `not_sent` are what they say;
 *  `uncertain`, no receipt and anything unexpected are all "not confirmed". */
export function deliveryOf(handoff: unknown): OutboxRow["delivery"] {
  return handoff === "sent" || handoff === "queued" || handoff === "not_sent"
    ? handoff
    : "unconfirmed";
}

const isMine = (t: RemoteTurn) => t.mine === true;

/** Tie each sent message to the conversation turn it became, once the owner shows one: a turn
 *  of OURS (`mine`) that was not there when the message was sent and carries the same text.
 *  Display only: it moves the delivery line under the turn instead of drawing the text twice.
 *
 *  A `not_sent` row is tied like any other: the owner's `not_sent` is a RECORDED turn the agent
 *  never got (the runtime answers the receipt from that turn), so the conversation shows it.
 *  The row therefore ends under its turn whether the reply or the snapshot came first, and the
 *  pane offers the same Send again there (same text, same `client_key`: the owner answers that
 *  one operation, a key never makes a second turn). */
function bound(outbox: OutboxRow[], turns: RemoteTurn[]): OutboxRow[] {
  if (!outbox.some((r) => !r.turn && r.text !== null)) return outbox;
  const taken = new Set(outbox.map((r) => r.turn).filter(Boolean));
  return outbox.map((row) => {
    if (row.turn || row.text === null) return row;
    const hit = turns.find(
      (t) =>
        isMine(t) &&
        !taken.has(t.turn_id) &&
        !row.prior.includes(t.turn_id) &&
        typeof t.text === "string" &&
        t.text.trim() === row.text!.trim(),
    );
    if (!hit) return row;
    taken.add(hit.turn_id);
    return { ...row, turn: hit.turn_id };
  });
}

/** When a leftover that just answered unknown is asked again. */
function nextCheckAt(firstUnknownAt: number, now: number): number | null {
  for (const offset of RECONCILE_RECHECK_MS) if (firstUnknownAt + offset > now) return firstUnknownAt + offset;
  return null;
}

/** The pane's answers to its own requests (#2061). The caller already matched the generation. */
function inputReduce(state: RemotePaneState, event: RemotePaneEvent): RemotePaneState {
  const leftover = (key: string, change: (l: Leftover) => Leftover | null): Leftover[] =>
    state.leftovers.flatMap((l) => {
      if (l.client_key !== key) return [l];
      const next = change(l);
      return next ? [next] : [];
    });
  const row = (key: string, change: (r: OutboxRow) => OutboxRow): OutboxRow[] =>
    state.outbox.map((r) => (r.client_key === key ? change(r) : r));
  switch (event.type) {
    case "suggestions":
      return state.status === "live" ? { ...state, suggestions: listedSuggestions(event.rows) } : state;
    case "suggested": {
      const made = suggestionRef(event.suggestion);
      if (!made || state.status !== "live") return state;
      const now = event.now / 1000;
      const rest = (state.suggestions ?? []).filter((x) => x.suggestion_id !== made.suggestion_id);
      const before = (state.suggestions ?? []).find((x) => x.suggestion_id === made.suggestion_id);
      return {
        ...state,
        held: false,
        suggestions: [
          {
            suggestion_id: made.suggestion_id,
            client_key: event.client_key,
            state: made.state,
            handoff: before?.handoff ?? null,
            created_at: before?.created_at ?? now,
            updated_at: now,
          },
          ...rest,
        ],
      };
    }
    case "withdrawn": {
      const gone = suggestionRef(event.suggestion);
      if (!gone || !state.suggestions) return state;
      return {
        ...state,
        suggestions: state.suggestions.map((x) =>
          x.suggestion_id === gone.suggestion_id
            ? { ...x, state: gone.state, updated_at: event.now / 1000 }
            : x,
        ),
      };
    }
    case "sending": {
      if (state.status !== "live") return state;
      if (state.outbox.some((r) => r.client_key === event.client_key))
        return { ...state, outbox: row(event.client_key, (r) => ({ ...r, delivery: "sending" })) };
      const prior = remoteTurns(state)
        .filter(isMine)
        .map((t) => t.turn_id);
      return {
        ...state,
        outbox: [
          ...state.outbox,
          { client_key: event.client_key, text: event.text, delivery: "sending", prior, turn: null },
        ],
      };
    }
    case "sent":
      return {
        ...state,
        held: false,
        outbox: bound(
          row(event.client_key, (r) => ({ ...r, delivery: deliveryOf(event.handoff) })),
          remoteTurns(state),
        ),
      };
    case "unconfirmed":
      return { ...state, outbox: row(event.client_key, (r) => ({ ...r, delivery: "unconfirmed" })) };
    case "unsent":
      // A definite refusal: nothing was delivered, and the text is still in the composer.
      return { ...state, outbox: state.outbox.filter((r) => r.client_key !== event.client_key) };
    case "held":
      return { ...state, held: event.held };
    case "refused":
      return { ...state, refusedRevision: event.revision };
    case "checking":
      return {
        ...state,
        leftovers: leftover(event.client_key, (l) =>
          l.phase === "final" ? l : { ...l, phase: "checking", nextAt: null },
        ),
      };
    case "giveup":
      // Two minutes of unknown: still "may or may not", and nothing more is asked.
      return {
        ...state,
        leftovers: leftover(event.client_key, (l) => ({ ...l, phase: "final", nextAt: null })),
      };
    case "status": {
      const found = state.leftovers.find((l) => l.client_key === event.client_key);
      if (!found || found.phase === "final") return state;
      const { result, now } = event;
      if (result === "failed")
        return {
          ...state,
          leftovers: leftover(event.client_key, (l) => ({
            ...l,
            phase: "failed",
            nextAt: now + RECONCILE_RETRY_MS,
          })),
        };
      if (!result || result.known !== true) {
        const first = found.firstUnknownAt ?? now;
        return {
          ...state,
          leftovers: leftover(event.client_key, (l) => ({
            ...l,
            phase: "unknown",
            firstUnknownAt: first,
            nextAt: nextCheckAt(first, now),
          })),
        };
      }
      // Known: the notice goes. A suggestion is in the list the owner answers; a sent message
      // is shown with its real state (its text was never stored, so there is none to show).
      const rest = leftover(event.client_key, () => null);
      if (result.kind !== "send" || state.outbox.some((r) => r.client_key === event.client_key))
        return { ...state, leftovers: rest };
      return {
        ...state,
        leftovers: rest,
        outbox: [
          ...state.outbox,
          {
            client_key: event.client_key,
            text: null,
            delivery: deliveryOf(result.handoff),
            prior: [],
            turn: null,
          },
        ],
      };
    }
    default:
      return state;
  }
}

/** The turns of an answer that can be shown: objects with an id, each id once. The server checks
 *  an answer's shape and depth, not every field; what a peer sent is never trusted to be well
 *  formed here either. */
function listedTurns(raw: unknown): RemoteTurn[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const out: RemoteTurn[] = [];
  for (const t of raw) {
    const id = (t as RemoteTurn | null)?.turn_id;
    if (!t || typeof t !== "object" || typeof id !== "string" || !id || seen.has(id)) continue;
    seen.add(id);
    out.push(t as RemoteTurn);
  }
  return out;
}

const text = (v: unknown): string | null => (typeof v === "string" ? v : null);
const objects = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v)
    ? (v.filter((x) => x && typeof x === "object" && !Array.isArray(x)) as Record<string, unknown>[])
    : [];

/** One turn from a peer as the viewer's turn component takes it, field by field. Anything that
 *  is not the type it should be is dropped, so a malformed answer renders as less, never as a
 *  crash. An attachment keeps its name and type and NEVER an image handle: nothing a peer sent
 *  can make this instance load a picture. */
export function remoteTurnView(raw: RemoteTurn): PublicTurn {
  const author = raw.author as
    | { name?: unknown; via?: unknown; edited?: unknown }
    | null
    | undefined;
  const omitted = raw.tools_omitted;
  return {
    kind: raw.kind === "agent" ? "agent" : "operator",
    turn_id: raw.turn_id,
    state: text(raw.state) ?? "",
    text: text(raw.text),
    reply: text(raw.reply),
    reply_ts: null,
    reply_truncated: raw.reply_truncated === true,
    attachments: objects(raw.attachments).map((a) => ({
      name: text(a.name) ?? "",
      mime: text(a.mime) ?? "file",
    })),
    tools: objects(raw.tools).map((t) => ({
      name: text(t.name),
      outcome: text(t.outcome),
      summary: text(t.summary),
    })),
    tools_truncated: raw.tools_truncated === true,
    tools_omitted: typeof omitted === "number" && omitted > 0 ? omitted : 0,
    author:
      author && author.via === "public_link" && typeof author.name === "string"
        ? { name: author.name, via: "public_link" }
        : author && author.via === "peer" && typeof author.name === "string"
          ? // A turn a peer caused (#2057): the owner's name for that peer and whether the
            // owner edited it. Text nodes only; the peer's own `actor` label is not shown.
            { name: author.name, via: "peer", ...(author.edited === true ? { edited: true } : {}) }
          : null,
    mine: raw.mine === true,
    truncated: raw.truncated === true,
  };
}

/** What a failed read means for the pane, from its HTTP status alone. */
export function remoteFailure(status: number): "gone" | "offline" | "stall" {
  if (status === 404) return "gone";
  if (status === 503) return "offline";
  return "stall";
}

/** The pane's whole state machine, pure. Content only ever enters through an answer whose
 *  generation is the current one; every transition that clears content bumps the generation, so
 *  a slow success can never refill a pane that was cleared or now shows another session. */
export function remotePaneReduce(state: RemotePaneState, event: RemotePaneEvent): RemotePaneState {
  if (event.type === "open")
    return event.target === state.target
      ? state
      : cleared(state, "loading", 0, event.target, event.leftovers);
  if (event.generation !== state.generation) return state; // an answer from before a clear
  if (event.type !== "failed" && event.type !== "snapshot" && event.type !== "history")
    return inputReduce(state, event);
  if (event.type === "failed") {
    const kind = remoteFailure(event.status);
    if (kind === "gone") return cleared(state, "gone", null);
    if (kind === "offline") return cleared(state, "offline", REMOTE_RETRY_MS);
    return state.stalls + 1 >= REMOTE_STALL_LIMIT
      ? cleared(state, "offline", REMOTE_RETRY_MS)
      : { ...state, stalls: state.stalls + 1 };
  }
  if (event.type === "snapshot") {
    const next = { ...event.snapshot, turns: listedTurns(event.snapshot.turns) };
    const ids = new Set(next.turns.map((t) => t.turn_id));
    // Turns already on screen that the newer window no longer carries stay, in order, once
    // history was paged in: the list must not grow a hole between `older` and the snapshot.
    const carried =
      state.older.length && state.snapshot
        ? state.snapshot.turns.filter((t) => !ids.has(t.turn_id))
        : [];
    const older = [...state.older.filter((t) => !ids.has(t.turn_id)), ...carried];
    return {
      ...state,
      status: "live",
      snapshot: next,
      older,
      hasMore: older.length ? state.hasMore : Number(next.omitted_turns) > 0,
      stalls: 0,
      outbox: bound(state.outbox, [...older, ...next.turns]),
    };
  }
  // history: a page of turns that end where the oldest held turn begins.
  if (state.status !== "live" || !state.snapshot) return state;
  const held = new Set([...state.older, ...state.snapshot.turns].map((t) => t.turn_id));
  return {
    ...state,
    older: [...listedTurns(event.page.turns).filter((t) => !held.has(t.turn_id)), ...state.older],
    hasMore: event.page.has_more === true,
    stalls: 0,
  };
}

/** Every turn the pane shows, oldest first. */
export function remoteTurns(state: RemotePaneState): RemoteTurn[] {
  return state.snapshot ? [...state.older, ...state.snapshot.turns] : [];
}

/** The banner's connection word and LED: green only for a connected peer. */
export function remotePaneBadge(
  status: RemotePaneStatus,
  stalled: boolean,
): { led: "up" | "idle"; word: string } {
  if (status === "live" && !stalled) return { led: "up", word: "connected" };
  if (status === "live") return { led: "idle", word: "reconnecting" };
  if (status === "gone") return { led: "idle", word: "unavailable" };
  if (status === "offline") return { led: "idle", word: "offline" };
  return { led: "idle", word: "connecting" };
}

export const REMOTE_GONE = "This share is no longer available.";
export const remoteOfflineText = (peer: string) =>
  `${peer} is not connected. Showing nothing until it is back.`;
export const remoteViewOnlyText = (peer: string) => `Ask ${peer}'s operator for more access.`;

// ---- suggesting and sending to a shared session (#2061) ---------------------------------------

export const remoteHeldText = (peer: string) =>
  `${peer}'s session is run by a mission right now. You can watch.`;
export const remoteAccessEndedText = (level: "suggest" | "send") =>
  `${level === "send" ? "Send" : "Suggest"} access ended. Your text is kept here; ask for access again.`;
export const RELOAD_UNKNOWN =
  "A message you were sending before the reload may or may not have arrived." +
  " Check the conversation before sending it again.";
export const RELOAD_UNCHECKED = "Could not check a message you were sending before the reload.";

export type InputLevel = "suggest" | "send";
export const inputLevel = (level: string | null | undefined): InputLevel | null =>
  level === "suggest" || level === "send" ? level : null;

export interface ComposerState {
  /** Which composer is drawn, if any. The owner decides on every call; this only chooses what
   *  to offer. */
  kind: InputLevel | null;
  enabled: boolean;
  placeholder: string;
  button: string;
  /** The line above a disabled composer saying why. */
  note: string | null;
}

/** The remote pane's composer, from the mirrored level and what the owner last answered.
 *
 *  `level` is the mirrored one right now (`null` when the share is no longer listed); `offered`
 *  is the input level this pane last offered for this share. Text typed under a level that then
 *  ended (lowered, expired, refused) is never thrown away: the composer stays, disabled, with
 *  the text in it. With nothing typed there is nothing to keep, and the pane is view only. */
export function composerState(input: {
  status: RemotePaneStatus;
  peer: string;
  /** The mirrored kind of the share. A terminal (#2060) is shared View only for good: it never
   *  has a composer, whatever level a row claims and whatever was offered before. */
  kind?: string | null;
  level: string | null;
  offered: InputLevel | null;
  expired: boolean;
  refused: boolean;
  held: boolean;
  hasText: boolean;
}): ComposerState {
  const { status, peer, held, hasText } = input;
  const none: ComposerState = { kind: null, enabled: false, placeholder: "", button: "", note: null };
  if (input.kind === "terminal") return none;
  const live = input.expired ? null : inputLevel(input.level);
  const kind = live ?? input.offered;
  if (!kind) return none;
  const base = {
    kind,
    placeholder:
      kind === "send" ? `Message ${peer}'s agent…` : `Suggest a message to ${peer}'s operator…`,
    button: kind === "send" ? "Send" : "Suggest",
  };
  // Ended: no level that writes any more (lowered to view, expired, no longer listed), the
  // owner refused a write under this revision, or the share is gone. A level lowered from send
  // to suggest is not "ended": it is simply the Suggest composer, with the text still in it.
  if (!live || input.refused || status === "gone")
    return hasText ? { ...base, enabled: false, note: remoteAccessEndedText(kind) } : none;
  if (status === "offline") return { ...base, enabled: false, note: null };
  if (held) return { ...base, enabled: false, note: remoteHeldText(peer) };
  return { ...base, enabled: status === "live", note: null };
}

export interface DeliveryLine {
  word: string;
  tone: "ok" | "idle";
  detail: string | null;
  /** Only a message the owner says did NOT reach the agent can be sent again, and only as the
   *  same message under the same key. */
  resend: boolean;
}

/** One presentation per delivery state. "Not confirmed" is its own state: never "sent", never
 *  "not sent", and never offered for resending. */
export function deliveryLine(delivery: OutboxRow["delivery"]): DeliveryLine {
  switch (delivery) {
    case "sending":
      return { word: "sending…", tone: "idle", detail: null, resend: false };
    case "sent":
      return { word: "sent", tone: "ok", detail: null, resend: false };
    case "queued":
      return { word: "queued", tone: "idle", detail: null, resend: false };
    case "not_sent":
      return { word: "not sent", tone: "idle", detail: "It did not reach the agent.", resend: true };
    default:
      return {
        word: "delivery not confirmed",
        tone: "idle",
        detail: "It may still arrive. Do not resend blindly.",
        resend: false,
      };
  }
}

export interface OwnSuggestionLine {
  word: string;
  tone: "ok" | "idle";
  withdraw: boolean;
}

/** A row of "Your suggestions". Grey while it waits: it is not THIS operator's attention that
 *  is needed. `approving` is never shown as delivered, `expired` never as rejected. */
export function ownSuggestionLine(state: string, peer: string): OwnSuggestionLine {
  switch (state) {
    case "pending":
      return { word: `waiting for ${peer}'s operator`, tone: "idle", withdraw: true };
    case "approving":
      return { word: "approved · being delivered", tone: "idle", withdraw: false };
    case "approved":
      return { word: "approved", tone: "ok", withdraw: false };
    case "rejected":
      return { word: "rejected", tone: "idle", withdraw: false };
    case "expired":
      return { word: "expired", tone: "idle", withdraw: false };
    default:
      return { word: "withdrawn", tone: "idle", withdraw: false };
  }
}

/** The notices a reloaded pane shows for requests it cannot account for, each text once. */
export function reloadNotices(leftovers: readonly Leftover[], status: RemotePaneStatus): string[] {
  const out = new Set<string>();
  for (const l of leftovers) {
    // Asking again about a key that already answered unknown does not take the notice away:
    // it stays until an answer says what became of the message.
    if (l.phase === "unknown" || l.phase === "final" || (l.phase === "checking" && l.firstUnknownAt !== null))
      out.add(RELOAD_UNKNOWN);
    // A pane that cannot ask (gone for good) could not check either.
    else if (l.phase === "failed" || status === "gone") out.add(RELOAD_UNCHECKED);
  }
  return [...out];
}

/** What the pane does next about its leftovers: which to ask about now, which to give up on,
 *  and how long until something is due (ms; `null` = nothing scheduled). Pure. */
export function reconcilePlan(
  leftovers: readonly Leftover[],
  now: number,
): { check: string[]; giveUp: string[]; wakeIn: number | null } {
  const check: string[] = [];
  const giveUp: string[] = [];
  let wake: number | null = null;
  const due = (at: number) => {
    wake = wake === null ? at : Math.min(wake, at);
  };
  for (const l of leftovers) {
    if (l.phase === "pending") check.push(l.client_key);
    else if (l.phase === "failed") {
      if ((l.nextAt ?? 0) <= now) check.push(l.client_key);
      else due(l.nextAt!);
    } else if (l.phase === "unknown") {
      const deadline = (l.firstUnknownAt ?? now) + RECONCILE_GIVE_UP_MS;
      if (now >= deadline) giveUp.push(l.client_key);
      else if (l.nextAt !== null && l.nextAt <= now) check.push(l.client_key);
      else due(Math.min(deadline, l.nextAt ?? deadline));
    }
  }
  return { check, giveUp, wakeIn: wake === null ? null : Math.max(0, wake - now) };
}

const OPS_KEY = "bl-remote-ops";
const OPS_MAX = 20;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function cleanOp(raw: unknown): PendingOp | null {
  const r = raw as Partial<PendingOp> | null;
  if (!r || typeof r !== "object") return null;
  if (typeof r.peer_id !== "string" || !PEER_ID_RE.test(r.peer_id)) return null;
  if (typeof r.grant_id !== "string" || !GRANT_ID_RE.test(r.grant_id)) return null;
  if (typeof r.client_key !== "string" || !UUID_RE.test(r.client_key)) return null;
  if (r.kind !== "send" && r.kind !== "suggest") return null;
  if (typeof r.started_at !== "number" || !Number.isFinite(r.started_at)) return null;
  // Rebuilt field by field: whatever else a record carried is not kept.
  return {
    peer_id: r.peer_id,
    grant_id: r.grant_id,
    client_key: r.client_key,
    kind: r.kind,
    started_at: r.started_at,
  };
}

/** The in-flight records (#2061): `{peer_id, grant_id, client_key, kind, started_at}` and
 *  NOTHING else. No text, no title, nothing from the conversation: this module is the only
 *  writer of the key, and it takes no other field. `sessionStorage`, so they end with the tab. */
export const pendingOps = {
  all(storage: Pick<Storage, "getItem"> = sessionStorage): PendingOp[] {
    try {
      const raw = JSON.parse(storage.getItem(OPS_KEY) ?? "[]");
      return Array.isArray(raw) ? (raw.map(cleanOp).filter(Boolean) as PendingOp[]) : [];
    } catch {
      return [];
    }
  },
  of(peerId: string, grantId: string, storage?: Pick<Storage, "getItem">): PendingOp[] {
    return this.all(storage).filter((op) => op.peer_id === peerId && op.grant_id === grantId);
  },
  add(op: PendingOp, storage: Pick<Storage, "getItem" | "setItem"> = sessionStorage): void {
    const clean = cleanOp(op);
    if (!clean) return;
    const rest = this.all(storage).filter((o) => o.client_key !== clean.client_key);
    try {
      storage.setItem(OPS_KEY, JSON.stringify([...rest, clean].slice(-OPS_MAX)));
    } catch {
      /* storage refused: the request goes on, a reload then cannot find out about it */
    }
  },
  remove(clientKey: string, storage: Pick<Storage, "getItem" | "setItem"> = sessionStorage): void {
    const all = this.all(storage);
    const rest = all.filter((o) => o.client_key !== clientKey);
    if (rest.length === all.length) return;
    try {
      storage.setItem(OPS_KEY, JSON.stringify(rest));
    } catch {
      /* nothing to do */
    }
  },
};

/** The retry key of the message in the composer: ONE UUID per composed message. A retry of the
 *  same text after a failure reuses it (the owner then answers the first attempt's outcome);
 *  different text is a different message and always gets a new key; so does the next message
 *  after one was accepted (`accepted`). A key is never reused for different text. */
export function draftKey(
  held: { text: string; key: string } | null,
  text: string,
  mint: () => string,
): { text: string; key: string } {
  return held && held.text === text ? held : { text, key: mint() };
}

/** What a refused or failed write means, from its HTTP status AND the server's code. Only an
 *  answer this table knows as a refusal is "nothing was accepted". Everything else (a missing
 *  answer, a gateway error, a timeout, a lost channel, a status or code not listed here) says
 *  nothing either way: it is never treated as non-delivery, and the in-flight record stays. */
export type WriteFailure =
  /** `404 no_grant`: the owner refused the write. Access to write ended (or the mirror is behind). */
  | "refused"
  /** `409 held`: a mission runs that session. */
  | "held"
  /** `503 peer_offline`: no channel when the request was made, so it never left this instance.
   *  A channel that ended AFTER the request was handed to it is `504 peer_lost`: unconfirmed. */
  | "offline"
  /** Another definite refusal: nothing was accepted; the server's words say why. */
  | "rejected"
  /** No answer, or one that cannot say what happened (network, `502`, `504 timeout`,
   *  `504 peer_lost`, `503` that is not `peer_offline`, `429 busy`, anything not listed).
   *
   *  `429 busy` is here on purpose (#2061). It is usually a refusal (five suggestions waiting, a
   *  rate limit), but an owner on a build before `answer_lost` also answered `busy` AFTER it
   *  delivered a message, when its byte budget for answers was spent. Two instances are updated
   *  independently, so this browser cannot tell the two apart: the record stays, nothing says
   *  "not sent", and the status of the same key says what happened. */
  | "unconfirmed";

/** The refusals a write route answers BEFORE or INSTEAD of an effect, by status then code.
 *  `401` and `403` are this instance turning the request away itself (no session, CSRF, the
 *  owner-only answer): whatever their code, nothing was passed on. */
const REFUSED_CODES: Record<number, readonly string[]> = {
  404: ["not_found"],
  409: ["conflict", "not_pending"],
  413: ["too_large"],
  422: ["invalid", "not_possible"],
};

export function writeFailure(status: number, code: unknown): WriteFailure {
  if (status === 404 && code === "no_grant") return "refused";
  if (status === 409 && code === "held") return "held";
  if (status === 503 && code === "peer_offline") return "offline";
  if (status === 401 || status === 403) return "rejected";
  if (typeof code === "string" && REFUSED_CODES[status]?.includes(code)) return "rejected";
  return "unconfirmed";
}

/** Whether the in-flight record of a request goes when it settled this way: an answer arrived
 *  and said nothing was accepted. With anything else the record stays, so a reload can still
 *  ask about the same key (`input.status`) instead of the message being composed again. */
export const settlesRecord = (failure: WriteFailure) => failure !== "unconfirmed";

export const SUGGEST_UNCONFIRMED =
  "Could not confirm that the suggestion arrived. Check Your suggestions before suggesting it again.";
