/** Sharing a session with a peer (#2055): the pure mapping behind the dialog, the remote rows and
 *  the read-only pane. The pane's whole state machine is `remotePaneReduce`, so "nothing stale"
 *  and the generation fence are provable here without a browser. */
import { describe, expect, test } from "vitest";
import {
  GRANT_EXPIRY_DAYS,
  GRANT_EXPIRY_DEFAULT,
  EPOCH_S_MAX,
  GRANT_ID_RE,
  PEER_DISCLOSURE,
  PEER_DISCLOSURE_DETAIL,
  PEER_ID_RE,
  REMOTE_EVENTS_WAIT_S,
  REMOTE_RETRY_MS,
  REMOTE_STALL_LIMIT,
  expiresInSeconds,
  grantFor,
  grantRowState,
  isEpochS,
  remoteFailure,
  remotePaneBadge,
  remotePaneInit,
  remotePaneReduce,
  remotePath,
  remoteRowState,
  remoteTurnView,
  remoteTurns,
  shareablePeers,
  shortDate,
  UNKNOWN_DATE,
  type FederationGrant,
  type FederationPeer,
  type RemotePaneState,
  type RemoteShare,
  type RemoteSnapshot,
  type RemoteTurn,
} from "./federation";

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const NOV_22 = Date.UTC(2026, 10, 22, 12) / 1000;

const grant = (over: Partial<FederationGrant> = {}): FederationGrant => ({
  grant_id: GRANT,
  session_key: "codex-api:0d0f6a52-1111-4222-8333-444455556666",
  peer_id: GPU,
  peer_name: "gpu-box",
  level: "view",
  expires_at: NOV_22,
  revision: 3,
  state: "live",
  created_at: NOV_22 - 7 * 86400,
  ended_at: null,
  end_reason: null,
  ...over,
});

const remote = (over: Partial<RemoteShare> = {}): RemoteShare => ({
  key: `peer:${GPU}/codex-api:x`,
  peer_id: GPU,
  peer_name: "gpu-box",
  connected: true,
  grant_id: GRANT,
  engine: "codex-api",
  native_id: "x",
  title: "Tune the render queue",
  level: "view",
  expires_at: NOV_22,
  revision: 1,
  seen_at: NOV_22 - 60,
  ...over,
});

const turn = (id: string, over: Record<string, unknown> = {}): RemoteTurn => ({
  turn_id: id,
  state: "completed",
  text: `ask ${id}`,
  reply: `answer ${id}`,
  tools: [],
  ...over,
});

const snap = (ids: string[], over: Partial<RemoteSnapshot> = {}): RemoteSnapshot => ({
  link: "l".repeat(24),
  revision: 5,
  event_cursor: 5,
  turns: ids.map((id) => turn(id)),
  omitted_turns: 0,
  pending_requests: [],
  can_send: false,
  ...over,
});

describe("the dialog's choices", () => {
  test("expiry is 1, 7, 30 or 90 days, and 7 unless the operator says otherwise", () => {
    expect([...GRANT_EXPIRY_DAYS]).toEqual([1, 7, 30, 90]);
    expect(GRANT_EXPIRY_DEFAULT).toBe(7);
    expect(expiresInSeconds(7)).toBe(604_800);
    expect(expiresInSeconds(90)).toBe(90 * 86400); // the server's maximum
  });

  test("the disclosure is the approved text, word for word", () => {
    expect(`${PEER_DISCLOSURE} ${PEER_DISCLOSURE_DETAIL}`).toBe(
      "The other instance sees this conversation as you see it here, without your secrets we" +
        " know of. A secret we don't know about is not hidden. It gets the turns and the tool" +
        " activity. Never the folder, the model settings or files; an image shows as its name only.",
    );
  });

  test("a revoked peer is not offered", () => {
    const peers = [
      { peer_id: "a", state: "trusted" },
      { peer_id: "b", state: "revoked" },
    ] as FederationPeer[];
    expect(shareablePeers(peers).map((p) => p.peer_id)).toEqual(["a"]);
  });

  test("sharing again with a peer finds its live grant, so the expiry is changed, not doubled", () => {
    const live = [grant({ grant_id: "one", peer_id: "x" }), grant()];
    expect(grantFor(live, GPU)?.grant_id).toBe(GRANT);
    expect(grantFor(live, "nobody")).toBeUndefined();
  });
});

describe("a grant row", () => {
  test("in a session's dialog it names the peer; in a peer's list, the session", () => {
    expect(grantRowState(grant(), "peer")).toEqual({
      title: "gpu-box",
      level: "View",
      until: "until 22 Nov",
    });
    expect(grantRowState(grant({ title: "Refactor the importer" }), "session").title).toBe(
      "Refactor the importer",
    );
  });

  test("a row is never blank: no name falls back to the id, no title to the key's tail", () => {
    expect(grantRowState(grant({ peer_name: "" }), "peer").title).toBe("m5tr2kxw");
    expect(grantRowState(grant({ title: "" }), "session").title).toBe("Session 0d0f6a52");
  });
});

// #2055 review (P1): a peer could list an expiry such as 1e300. It is finite, so the old
// `typeof === "number"` let it through, and `Intl.DateTimeFormat#format(new Date(1e303))` throws
// `RangeError: Invalid time value` — from a row drawn outside the pane's error boundary.
const HOSTILE_EXPIRIES = [1e300, Number.MAX_VALUE, EPOCH_S_MAX + 1, Infinity, -Infinity, NaN, -1, 0];

describe("an expiry a peer listed is formatted safely", () => {
  test("a date this browser can build is a date; anything else reads neutrally, never a throw", () => {
    expect(shortDate(NOV_22)).toBe("22 Nov");
    expect(isEpochS(NOV_22)).toBe(true);
    expect(isEpochS(EPOCH_S_MAX)).toBe(true);
    expect(() => shortDate(EPOCH_S_MAX)).not.toThrow();
    for (const v of [...HOSTILE_EXPIRIES, "1800000000", null, undefined, {}]) {
      expect(isEpochS(v), String(v)).toBe(false);
      expect(shortDate(v), String(v)).toBe(UNKNOWN_DATE);
    }
  });

  test("a row, a grant row and the map node's date already stored with such a value never throw", () => {
    for (const expires_at of HOSTILE_EXPIRIES) {
      const row = remoteRowState(remote({ expires_at }));
      expect(row.meta).toBe(`view only · until ${UNKNOWN_DATE}`);
      expect(row.until).toBe(`until ${UNKNOWN_DATE}`);
      expect(grantRowState(grant({ expires_at }), "peer").until).toBe(`until ${UNKNOWN_DATE}`);
    }
  });
});

describe("a remote row", () => {
  test("shows what the list read carries: title, peer, view only and the end date", () => {
    expect(remoteRowState(remote())).toEqual({
      title: "Tune the render queue",
      peer: "gpu-box",
      offline: false,
      screen: false,
      meta: "view only · until 22 Nov",
      until: "until 22 Nov",
      label: "Open Tune the render queue, shared by gpu-box, view only",
    });
  });

  test("says offline, and nothing else, while the peer's channel is down", () => {
    const row = remoteRowState(remote({ connected: false }));
    expect(row.offline).toBe(true);
    expect(row.meta).toBe("offline");
  });

  test("an untitled session and an unnamed peer still read as something", () => {
    const row = remoteRowState(remote({ title: "", peer_name: "" }));
    expect(row.title).toBe("(untitled)");
    expect(row.peer).toBe("m5tr2kxw");
  });

  test("its route is its own, and only ids of the server's shape are ever asked for", () => {
    expect(remotePath(GPU, GRANT)).toBe(`/remote/${GPU}/${GRANT}`);
    expect(remotePath("a/b", "c d")).toBe("/remote/a%2Fb/c%20d");
    expect(PEER_ID_RE.test(GPU)).toBe(true);
    expect(PEER_ID_RE.test("../../etc/passwd")).toBe(false);
    expect(GRANT_ID_RE.test(GRANT)).toBe(true);
    expect(GRANT_ID_RE.test("short")).toBe(false);
  });
});

describe("the pane's state from 200 / 404 / 503", () => {
  const open = (): RemotePaneState => remotePaneInit(`${GPU}/${GRANT}`);
  const live = (ids = ["t1", "t2"]): RemotePaneState =>
    remotePaneReduce(open(), { type: "snapshot", generation: 0, snapshot: snap(ids) });

  test("a status maps to what it means, and only 404 and 503 clear", () => {
    expect(remoteFailure(404)).toBe("gone");
    expect(remoteFailure(503)).toBe("offline");
    for (const s of [0, 409, 422, 429, 500, 502, 504]) expect(remoteFailure(s)).toBe("stall");
  });

  test("200: the turns are shown and the pane is live", () => {
    const s = live();
    expect(s.status).toBe("live");
    expect(remoteTurns(s).map((t) => t.turn_id)).toEqual(["t1", "t2"]);
    expect(remotePaneBadge(s.status, false)).toEqual({ led: "up", word: "connected" });
  });

  test("503: the turns are dropped, the pane says offline and asks again later", () => {
    const s = remotePaneReduce(live(), { type: "failed", generation: 0, status: 503 });
    expect(s.status).toBe("offline");
    expect(s.snapshot).toBeNull();
    expect(remoteTurns(s)).toEqual([]);
    expect(s.generation).toBe(1);
    expect(s.startAfterMs).toBe(REMOTE_RETRY_MS);
    expect(remotePaneBadge(s.status, false)).toEqual({ led: "idle", word: "offline" });
    // ...and it fills again when the peer is back, under the new generation.
    const back = remotePaneReduce(s, { type: "snapshot", generation: 1, snapshot: snap(["t3"]) });
    expect(back.status).toBe("live");
    expect(remoteTurns(back).map((t) => t.turn_id)).toEqual(["t3"]);
  });

  test("404: the turns are dropped for good and nothing is asked again", () => {
    const s = remotePaneReduce(live(), { type: "failed", generation: 0, status: 404 });
    expect(s.status).toBe("gone");
    expect(remoteTurns(s)).toEqual([]);
    expect(s.startAfterMs).toBeNull();
    expect(remotePaneBadge(s.status, false)).toEqual({ led: "idle", word: "unavailable" });
  });

  test("a failure that says nothing about the grant keeps the turns, three times at most", () => {
    let s = live();
    for (let i = 1; i < REMOTE_STALL_LIMIT; i++) {
      s = remotePaneReduce(s, { type: "failed", generation: 0, status: 504 });
      expect(s.status).toBe("live");
      expect(s.stalls).toBe(i);
      expect(remoteTurns(s)).toHaveLength(2);
      expect(remotePaneBadge(s.status, s.stalls > 0).word).toBe("reconnecting");
    }
    s = remotePaneReduce(s, { type: "failed", generation: 0, status: 504 });
    expect(s.status).toBe("offline"); // what cannot be confirmed is not kept on screen
    expect(remoteTurns(s)).toEqual([]);
    // A success in between resets the count.
    const ok = remotePaneReduce(
      remotePaneReduce(live(), { type: "failed", generation: 0, status: 504 }),
      { type: "snapshot", generation: 0, snapshot: snap(["t1"]) },
    );
    expect(ok.stalls).toBe(0);
  });
});

describe("the generation fence", () => {
  const TARGET = `${GPU}/${GRANT}`;
  const live = remotePaneReduce(remotePaneInit(TARGET), {
    type: "snapshot",
    generation: 0,
    snapshot: snap(["t1"]),
  });

  test("a success that arrives after the pane cleared (404) is discarded", () => {
    const gone = remotePaneReduce(live, { type: "failed", generation: 0, status: 404 });
    const late = remotePaneReduce(gone, { type: "snapshot", generation: 0, snapshot: snap(["t1"]) });
    expect(late).toBe(gone);
    expect(remoteTurns(late)).toEqual([]);
  });

  test("a success that arrives after the pane cleared (503) is discarded", () => {
    const off = remotePaneReduce(live, { type: "failed", generation: 0, status: 503 });
    expect(remotePaneReduce(off, { type: "snapshot", generation: 0, snapshot: snap(["t1"]) })).toBe(off);
    expect(
      remotePaneReduce(off, {
        type: "history",
        generation: 0,
        page: { link: "l", turns: [turn("t0")], before: null, has_more: false },
      }),
    ).toBe(off);
  });

  test("switching to another shared session clears at once and refuses the old one's answers", () => {
    const other = remotePaneReduce(live, { type: "open", target: `${GPU}/another-grant-000000` });
    expect(other.generation).toBe(1);
    expect(other.status).toBe("loading");
    expect(remoteTurns(other)).toEqual([]);
    const late = remotePaneReduce(other, { type: "snapshot", generation: 0, snapshot: snap(["t1"]) });
    expect(late).toBe(other);
    // The new session's own answer is taken.
    const mine = remotePaneReduce(other, { type: "snapshot", generation: 1, snapshot: snap(["n1"]) });
    expect(remoteTurns(mine).map((t) => t.turn_id)).toEqual(["n1"]);
  });

  test("opening the same target again changes nothing", () => {
    expect(remotePaneReduce(live, { type: "open", target: TARGET })).toBe(live);
  });

  test("an old failure cannot clear a newer generation either", () => {
    const off = remotePaneReduce(live, { type: "failed", generation: 0, status: 503 });
    const back = remotePaneReduce(off, { type: "snapshot", generation: 1, snapshot: snap(["t9"]) });
    expect(remotePaneReduce(back, { type: "failed", generation: 0, status: 404 })).toBe(back);
  });
});

describe("history", () => {
  const TARGET = `${GPU}/${GRANT}`;
  const start = remotePaneReduce(remotePaneInit(TARGET), {
    type: "snapshot",
    generation: 0,
    snapshot: snap(["t3", "t4"], { omitted_turns: 2 }),
  });

  test("older turns exist when the snapshot left some out", () => {
    expect(start.hasMore).toBe(true);
    expect(
      remotePaneReduce(remotePaneInit(TARGET), { type: "snapshot", generation: 0, snapshot: snap(["a"]) })
        .hasMore,
    ).toBe(false);
  });

  test("a page goes in front, oldest first, and says whether more remain", () => {
    const paged = remotePaneReduce(start, {
      type: "history",
      generation: 0,
      page: { link: "l", turns: [turn("t1"), turn("t2"), turn("t3")], before: "t1", has_more: false },
    });
    expect(remoteTurns(paged).map((t) => t.turn_id)).toEqual(["t1", "t2", "t3", "t4"]); // t3 once
    expect(paged.hasMore).toBe(false);
  });

  test("a newer snapshot keeps the paged turns in order, without a hole or a repeat", () => {
    const paged = remotePaneReduce(start, {
      type: "history",
      generation: 0,
      page: { link: "l", turns: [turn("t1"), turn("t2")], before: "t1", has_more: false },
    });
    // The window moved on: t3 fell out of it, t5 arrived.
    const next = remotePaneReduce(paged, {
      type: "snapshot",
      generation: 0,
      snapshot: snap(["t4", "t5"], { omitted_turns: 3 }),
    });
    expect(remoteTurns(next).map((t) => t.turn_id)).toEqual(["t1", "t2", "t3", "t4", "t5"]);
    expect(next.hasMore).toBe(false);
  });

  test("a page for a pane that is not live is ignored", () => {
    const loading = remotePaneInit(TARGET);
    expect(
      remotePaneReduce(loading, {
        type: "history",
        generation: 0,
        page: { link: "l", turns: [turn("t1")], before: null, has_more: false },
      }),
    ).toBe(loading);
  });
});

describe("what a peer sent is never trusted to be well formed", () => {
  test("a snapshot's turns must be objects with an id, each once", () => {
    const s = remotePaneReduce(remotePaneInit("x"), {
      type: "snapshot",
      generation: 0,
      snapshot: snap([], {
        turns: [turn("a"), null, "x", { text: "no id" }, turn("a"), turn("b")] as RemoteTurn[],
      }),
    });
    expect(remoteTurns(s).map((t) => t.turn_id)).toEqual(["a", "b"]);
    const none = remotePaneReduce(remotePaneInit("x"), {
      type: "snapshot",
      generation: 0,
      snapshot: { ...snap([]), turns: "nope" as unknown as RemoteTurn[] },
    });
    expect(remoteTurns(none)).toEqual([]);
  });

  test("a turn is rebuilt field by field: wrong types are dropped, not rendered", () => {
    const view = remoteTurnView(
      turn("t1", {
        kind: "weird",
        state: 7,
        text: { toString: () => "<b>x</b>" },
        reply: ["a"],
        tools: "rm -rf",
        attachments: [null, 4, { name: 9, mime: "image/png", image: "handle" }],
        author: { name: 5, via: "public_link" },
        truncated: "yes",
      }),
    );
    expect(view).toEqual({
      kind: "operator",
      turn_id: "t1",
      state: "",
      text: null,
      reply: null,
      reply_ts: null,
      reply_truncated: false,
      attachments: [{ name: "", mime: "image/png" }],
      tools: [],
      tools_truncated: false,
      tools_omitted: 0,
      author: null,
      truncated: false,
    });
  });

  test("an attachment never keeps an image handle: a picture from a peer is not loaded", () => {
    const view = remoteTurnView(
      turn("t1", { attachments: [{ name: "diagram.png", mime: "image/png", image: "abc" }] }),
    );
    expect(view.attachments).toEqual([{ name: "diagram.png", mime: "image/png" }]);
    expect(JSON.stringify(view)).not.toContain("abc");
  });

  test("a clipped turn keeps its cut marker and its omitted steps", () => {
    const view = remoteTurnView(
      turn("t1", {
        truncated: true,
        tools_omitted: 4,
        tools: [{ name: "read", summary: "a.py", outcome: "ok" }],
      }),
    );
    expect(view.truncated).toBe(true);
    expect(view.tools_omitted).toBe(4);
    expect(view.tools).toEqual([{ name: "read", summary: "a.py", outcome: "ok" }]);
  });
});

test("an events read is held 3 s: under Home Free's 5 s proxy read timeout", () => {
  expect(REMOTE_EVENTS_WAIT_S).toBe(3);
  expect(REMOTE_EVENTS_WAIT_S).toBeLessThan(5);
});
