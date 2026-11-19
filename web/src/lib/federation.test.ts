import { describe, expect, test } from "vitest";
import {
  formatCountdown,
  groupCode,
  groupPeerId,
  inviteCardKind,
  PAIRING_GONE_NOT_STORED,
  PAIRING_GONE_STORED,
  PAIRING_GONE_UNKNOWN,
  pairingCardKind,
  peerRowState,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  pollInterval,
  reachableLabel,
  searchRemoteShares,
  sentence,
  settledMessage,
  trackPairings,
  type FederationPairing,
  type FederationPeer,
  type RemoteShare,
} from "./federation";

const NOW = 1_800_000_000;

const peer = (over: Partial<FederationPeer> = {}): FederationPeer => ({
  peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
  name: "gpu-box",
  state: "trusted",
  connected: true,
  refused: false,
  reachable_by: ["direct"],
  created_at: NOW - 86400,
  last_seen_at: NOW - 2 * 3600,
  revision: 1,
  ...over,
});

const pairing = (over: Partial<FederationPairing> = {}): FederationPairing => ({
  id: "p1",
  role: "acceptor",
  state: "confirm",
  code: "482915",
  peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
  name: "gpu-box",
  local_confirmed: false,
  remote_confirmed: false,
  expires_at: NOW + 600,
  error: "",
  ...over,
});

describe("formatting", () => {
  test("a peer id reads in groups of four", () => {
    expect(groupPeerId("k3wq7n2bxd5mr4tzh6pac2vyj7uef3so")).toEqual([
      "k3wq", "7n2b", "xd5m", "r4tz", "h6pa", "c2vy", "j7ue", "f3so",
    ]);
    expect(groupPeerId("abcdef")).toEqual(["abcd", "ef"]);
    expect(groupPeerId("")).toEqual([]);
  });

  test("the pairing code reads as two groups of three", () => {
    expect(groupCode("482915")).toEqual(["482", "915"]);
    expect(groupCode("")).toEqual([]);
  });

  test("the countdown is m:ss, rounds up to the second and never goes negative", () => {
    expect(formatCountdown(NOW + 581, NOW)).toBe("9:41");
    expect(formatCountdown(NOW + 600, NOW)).toBe("10:00");
    expect(formatCountdown(NOW + 59.2, NOW)).toBe("1:00");
    expect(formatCountdown(NOW + 5, NOW)).toBe("0:05");
    expect(formatCountdown(NOW + 0.4, NOW)).toBe("0:01");
    expect(formatCountdown(NOW, NOW)).toBe("0:00");
    expect(formatCountdown(NOW - 30, NOW)).toBe("0:00");
  });

  test("how this instance is reachable is a word about the kind, never an address", () => {
    expect(reachableLabel(["direct"])).toBe("directly");
    expect(reachableLabel(["relay"])).toBe("through the relay");
    expect(reachableLabel(["direct", "relay"])).toBe("directly and through the relay");
    expect(reachableLabel([])).toBeNull();
  });

  test("a server reason becomes a sentence", () => {
    expect(sentence("the pairing timed out")).toBe("The pairing timed out.");
    expect(sentence("Already a sentence.")).toBe("Already a sentence.");
    expect(sentence("  ")).toBe("");
  });

});

describe("peer rows", () => {
  test("connected: green, with a word, rename and revoke", () => {
    expect(peerRowState(peer(), true, NOW)).toEqual({
      kind: "connected",
      led: "up",
      word: "connected",
      canRename: true,
      revokeLabel: "Revoke",
    });
  });

  test("offline: grey, says when it was last seen", () => {
    const row = peerRowState(peer({ connected: false }), true, NOW);
    expect(row).toMatchObject({ kind: "offline", led: "idle", canRename: true, revokeLabel: "Revoke" });
    expect(row.word).toBe("offline · last seen 2 h ago");
    expect(peerRowState(peer({ connected: false, last_seen_at: null }), true, NOW).word).toBe(
      "offline · not seen yet",
    );
  });

  test("removed by the other side: amber with a word, and the one action is Remove", () => {
    expect(peerRowState(peer({ connected: false, refused: true }), true, NOW)).toEqual({
      kind: "refused",
      led: "att",
      word: "removed by the other side",
      canRename: false,
      revokeLabel: "Remove",
    });
  });

  test("revoked: settled and quiet, no action, never red", () => {
    expect(peerRowState(peer({ state: "revoked", connected: false }), true, NOW)).toEqual({
      kind: "revoked",
      led: "idle",
      word: "revoked",
      canRename: false,
      revokeLabel: null,
    });
    // also from a process that does not own federation
    expect(peerRowState(peer({ state: "revoked", connected: null }), false, NOW).kind).toBe("revoked");
  });

  test("a process that does not own federation claims neither connected nor offline", () => {
    for (const connected of [null, true, false]) {
      const row = peerRowState(peer({ connected }), false, NOW);
      expect(row).toEqual({
        kind: "unknown",
        led: "idle",
        word: "connection not shown here",
        canRename: false,
        revokeLabel: "Revoke",
      });
    }
    // An owner that answers null (it should not) is not turned into "offline" either.
    expect(peerRowState(peer({ connected: null }), true, NOW).kind).toBe("unknown");
  });
});

describe("the pairing card", () => {
  test("is the server's state, with `waiting` once this side confirmed", () => {
    expect(pairingCardKind(pairing({ state: "dialing", code: "" }))).toBe("dialing");
    expect(pairingCardKind(pairing())).toBe("confirm");
    expect(pairingCardKind(pairing({ local_confirmed: true }))).toBe("waiting");
    expect(pairingCardKind(pairing({ remote_confirmed: true }))).toBe("confirm");
    for (const state of ["done", "denied", "failed", "expired"] as const)
      expect(pairingCardKind(pairing({ state }))).toBe(state);
    expect(pairingCardKind({ ...pairing(), gone: true })).toBe("gone");
  });

  test("a settled pairing says the server's reason, then that nothing was stored", () => {
    expect(
      settledMessage(
        pairing({ state: "failed", error: "the other instance refused this invite (used, expired or wrong)" }),
      ),
    ).toBe("The other instance refused this invite (used, expired or wrong). Nothing was stored.");
    expect(settledMessage(pairing({ state: "expired", error: "the pairing timed out" }))).toBe(
      "The pairing timed out. Nothing was stored.",
    );
    expect(settledMessage(pairing({ state: "denied", error: "the other operator declined" }))).toBe(
      "The other operator declined. Nothing was stored.",
    );
    expect(settledMessage(pairing({ state: "failed" }))).toBe(
      "The pairing did not complete. Nothing was stored.",
    );
    expect(settledMessage({ ...pairing(), gone: true, stored: false })).toBe(PAIRING_GONE_NOT_STORED);
    expect(settledMessage({ ...pairing(), gone: true, stored: true })).toBe(PAIRING_GONE_STORED);
    expect(settledMessage({ ...pairing(), gone: true })).toBe(PAIRING_GONE_UNKNOWN);
  });

  test("an open pairing in a read is followed; a settled stranger is not", () => {
    expect(trackPairings([], [pairing({ id: "a" }), pairing({ id: "b", state: "failed" })], [])).toEqual([
      pairing({ id: "a" }),
    ]);
  });

  test("a followed pairing takes the server's newer state, through to settled", () => {
    const a = pairing({ id: "a", state: "dialing", code: "" });
    const confirm = pairing({ id: "a" });
    const failed = pairing({ id: "a", state: "failed", code: "", error: "the other instance went away" });
    expect(trackPairings([a], [confirm], [])).toEqual([confirm]);
    expect(trackPairings([confirm], [failed], [])).toEqual([failed]);
  });

  test("an OPEN followed pairing missing from a read is gone, and says what the peer list shows", () => {
    const open = pairing({ id: "a", local_confirmed: true });
    const other = { peer_id: "zzzz", state: "trusted" as const };
    // a restart: the instance is not among the peers
    const next = trackPairings([open], [], [other]);
    expect(next).toHaveLength(1);
    expect(pairingCardKind(next[0])).toBe("gone");
    expect(settledMessage(next[0])).toBe(PAIRING_GONE_NOT_STORED);
    // and it stays that way on the reads after it
    expect(trackPairings(next, [], [other])).toEqual(next);
  });

  test("completed while nobody read, then swept: the peer is trusted, so the card says stored", () => {
    const open = pairing({ id: "a", local_confirmed: true });
    const mine = { peer_id: open.peer_id, state: "trusted" as const };
    const next = trackPairings([open], [], [mine]);
    expect(pairingCardKind(next[0])).toBe("gone");
    expect(settledMessage(next[0])).toBe(PAIRING_GONE_STORED);
    expect(settledMessage(next[0])).not.toContain("Nothing was stored");
    // a revoked row is not "in your list" as a peer you trust
    const revoked = trackPairings([open], [], [{ peer_id: open.peer_id, state: "revoked" as const }]);
    expect(settledMessage(revoked[0])).toBe(PAIRING_GONE_NOT_STORED);
    // a later read corrects the card in either direction
    expect(settledMessage(trackPairings(revoked, [], [mine])[0])).toBe(PAIRING_GONE_STORED);
  });

  test("a gone pairing with no identity claims no outcome", () => {
    const next = trackPairings([pairing({ id: "a", peer_id: "" })], [], []);
    expect(settledMessage(next[0])).toBe(PAIRING_GONE_UNKNOWN);
  });

  test("a SETTLED followed pairing the server stopped listing keeps its reason", () => {
    const failed = pairing({ id: "a", state: "failed", error: "the other instance went away" });
    expect(trackPairings([failed], [], [])).toEqual([failed]);
  });
});

describe("the invite card", () => {
  const invite = { id: "inv1", expires_at: NOW + 600 };

  test("open while the server lists it and its time is not up", () => {
    expect(inviteCardKind(invite, { invites: [invite], pairings: [] }, NOW)).toBe("open");
  });

  test("taken when a pairing carries its id (the other instance used it)", () => {
    expect(
      inviteCardKind(invite, { invites: [], pairings: [pairing({ id: "inv1", role: "inviter" })] }, NOW),
    ).toBe("taken");
  });

  test("expired once its time is up, listed or not", () => {
    expect(inviteCardKind(invite, { invites: [], pairings: [] }, NOW + 600)).toBe("expired");
    expect(inviteCardKind(invite, { invites: [invite], pairings: [] }, NOW + 601)).toBe("expired");
  });

  test("gone when the server no longer lists it and nothing took it (a restart)", () => {
    expect(inviteCardKind(invite, { invites: [], pairings: [] }, NOW + 5)).toBe("gone");
  });
});

describe("polling", () => {
  test("fast only while the other operator can change something", () => {
    expect(pollInterval(false, [])).toBe(POLL_IDLE_MS);
    expect(pollInterval(true, [])).toBe(POLL_ACTIVE_MS);
    expect(pollInterval(false, [pairing()])).toBe(POLL_ACTIVE_MS);
    expect(pollInterval(false, [pairing({ state: "dialing" })])).toBe(POLL_ACTIVE_MS);
    expect(pollInterval(false, [pairing({ state: "failed" })])).toBe(POLL_IDLE_MS);
    expect(pollInterval(false, [{ ...pairing(), gone: true }])).toBe(POLL_IDLE_MS);
    expect(POLL_ACTIVE_MS).toBe(2000);
    expect(POLL_IDLE_MS).toBe(15000);
  });
});

describe("the Shared tab's search (#2198)", () => {
  const share = (title: string, peer_name: string, grant_id: string): RemoteShare => ({
    key: grant_id,
    peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
    peer_name,
    connected: true,
    grant_id,
    engine: "e",
    native_id: grant_id,
    title,
    level: "view",
    expires_at: NOW + 86400,
    revision: 1,
    seen_at: NOW,
  });
  const rows = [
    share("Migrate the billing tables", "gpu-box", "g1"),
    share("Bench the new tokenizer", "gpu-box", "g2"),
    share("Release notes 0.21", "Laptop", "g3"),
    share("", "", "g4"),
  ];
  const found = (q: string) => searchRemoteShares(rows, q).map((r) => r.grant_id);

  test("no text is the whole list, the same array", () => {
    expect(searchRemoteShares(rows, "")).toBe(rows);
    expect(searchRemoteShares(rows, "   ")).toBe(rows);
  });
  test("by title, whatever the case", () => {
    expect(found("billing")).toEqual(["g1"]);
    expect(found("BILLING")).toEqual(["g1"]);
    expect(found(" tokenizer ")).toEqual(["g2"]);
  });
  test("by peer name, whatever the case", () => {
    expect(found("gpu")).toEqual(["g1", "g2"]);
    expect(found("laptop")).toEqual(["g3"]);
    expect(found("LAPTOP")).toEqual(["g3"]);
  });
  test("by what the row shows for a session with no title and a peer with no name", () => {
    expect(found("untitled")).toEqual(["g4"]);
    expect(found("m5tr2kxw")).toEqual(["g4"]);
  });
  test("nothing else of a share is searched", () => {
    expect(found("view")).toEqual([]);
    expect(found("g1")).toEqual([]);
    expect(found("xyz")).toEqual([]);
  });
});
