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
}

export interface FederationGrantList {
  grants: FederationGrant[];
  generation: number;
  levels: string[];
  default_expires_in: Record<string, number>;
  max_expires_in: number;
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
  "No screen is sent after a control we can't redraw exactly, such as a backspace in a shell, or" +
  " while the screen shows any character outside plain Latin, Greek and Cyrillic text," +
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

/** "View" for `view`; a level this build does not know is shown as the server named it. */
export function levelLabel(level: string): string {
  return level ? level[0].toUpperCase() + level.slice(1) : "";
}

export interface GrantRowState {
  /** Who (in a session's dialog) or what (in a peer's list) the row is about. */
  title: string;
  level: string;
  until: string;
}

/** One row of "Shared with": in a session's dialog it names the peer, in a peer's list the
 *  session. A session without a readable title is shown by its key's short tail, never blank. */
export function grantRowState(grant: FederationGrant, by: "peer" | "session"): GrantRowState {
  const title =
    by === "peer"
      ? grant.peer_name || grant.peer_id.slice(0, 8)
      : grant.title || `Session ${grant.session_key.split(":").pop()?.slice(0, 8) ?? ""}`.trim();
  return { title, level: levelLabel(grant.level), until: `until ${shortDate(grant.expires_at)}` };
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

/** What a remote row (list or map) shows: exactly what the list read carries. */
export function remoteRowState(r: RemoteShare): RemoteRowState {
  const title = r.title || "(untitled)";
  const peer = r.peer_name || r.peer_id.slice(0, 8);
  const screen = shareKind(r) === "terminal";
  const access = screen
    ? "screen only"
    : r.level === "view"
      ? "view only"
      : levelLabel(r.level).toLowerCase();
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
}

export type RemotePaneEvent =
  | { type: "open"; target: string }
  | { type: "snapshot"; generation: number; snapshot: RemoteSnapshot }
  | { type: "history"; generation: number; page: RemoteHistoryPage }
  | { type: "failed"; generation: number; status: number };

export function remotePaneInit(target: string): RemotePaneState {
  return {
    target,
    generation: 0,
    status: "loading",
    startAfterMs: 0,
    snapshot: null,
    older: [],
    hasMore: false,
    stalls: 0,
  };
}

function cleared(
  state: RemotePaneState,
  status: RemotePaneStatus,
  startAfterMs: number | null,
  target = state.target,
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
  };
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
  const author = raw.author as { name?: unknown; via?: unknown } | null | undefined;
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
        : null,
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
    return event.target === state.target ? state : cleared(state, "loading", 0, event.target);
  if (event.generation !== state.generation) return state; // an answer from before a clear
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
