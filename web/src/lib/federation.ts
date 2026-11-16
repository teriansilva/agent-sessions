/** Federation (#1246 P2, #2052): the shapes `GET /api/federation` answers with, and the pure
 *  mapping from them to what Settings → Peers shows. No React here, so every state the page can be
 *  in is a function of plain data and is unit-tested (`federation.test.ts`).
 *
 *  The server contract is docs/invariants/federation.md. */
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
