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
    expect(grantRowState(grant(), "peer", NOV_22 - 6 * 86400)).toEqual({
      title: "gpu-box",
      level: "View",
      until: "until 22 Nov",
      countdown: false,
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
      mine: false,
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

// ---- suggesting and sending to a shared session (#2061) ---------------------------------------
import {
  RECONCILE_GIVE_UP_MS,
  RELOAD_UNCHECKED,
  RELOAD_UNKNOWN,
  composerState,
  deliveryLine,
  deliveryOf,
  draftKey,
  endsIn,
  grantEnds,
  ownSuggestionLine,
  pendingOps,
  reconcilePlan,
  reloadNotices,
  remoteAccessWords,
  settlesRecord,
  shortTime,
  UNKNOWN_TIME,
  writeFailure,
  type PendingOp,
} from "./federation";

describe("a countdown, from the server's expires_at", () => {
  test("minutes inside the hour, hours and minutes inside the day, never a negative", () => {
    expect(endsIn(1000 + 42 * 60 + 10, 1000)).toBe("ends in 42 min");
    expect(endsIn(1000 + 3 * 3600 + 5 * 60, 1000)).toBe("ends in 3 h 5 min");
    expect(endsIn(1000 + 4 * 3600, 1000)).toBe("ends in 4 h");
    expect(endsIn(1000 + 59, 1000)).toBe("ends in under a minute");
    expect(endsIn(1000, 1000)).toBe("ended");
    expect(endsIn(900, 1000)).toBe("ended");
  });

  test("a grant ending within a day counts down; a later one shows its date", () => {
    expect(grantEnds(NOV_22, NOV_22 - 42 * 60)).toEqual({ text: "ends in 42 min", countdown: true });
    expect(grantEnds(NOV_22, NOV_22 - 86399)).toMatchObject({ countdown: true });
    expect(grantEnds(NOV_22, NOV_22 - 86400)).toEqual({ text: "until 22 Nov", countdown: false });
    const row = grantRowState(grant({ level: "send" }), "peer", NOV_22 - 42 * 60);
    expect(row).toMatchObject({ level: "Send", until: "ends in 42 min", countdown: true });
  });

  test("an expiry that is not a date is neither a countdown nor 'ended', and never throws", () => {
    for (const v of [...HOSTILE_EXPIRIES, "1800000000", null, undefined, {}]) {
      expect(grantEnds(v, NOV_22), String(v)).toEqual({ text: `until ${UNKNOWN_DATE}`, countdown: false });
    }
    // a clock that is not one either: the date is not guessed from it
    expect(grantEnds(NOV_22, NaN)).toEqual({ text: `until ${UNKNOWN_DATE}`, countdown: false });
  });

  test("a peer's time (a suggestion's created_at) is HH:MM, or an unknown time; never a throw", () => {
    expect(shortTime(NOV_22)).toMatch(/^\d\d:\d\d$/);
    for (const v of [...HOSTILE_EXPIRIES, "14:05", null, undefined, {}]) {
      expect(() => shortTime(v)).not.toThrow();
      expect(shortTime(v), String(v)).toBe(UNKNOWN_TIME);
    }
  });

  test("the banner and the row say what the mirrored level offers", () => {
    expect(remoteAccessWords("view")).toBe("view only");
    expect(remoteAccessWords("suggest")).toBe("can suggest");
    expect(remoteAccessWords("send")).toBe("can send");
    expect(remoteAccessWords("steer")).toBe("steer"); // a level of a newer build, as named
    expect(remoteRowState(remote({ level: "send" })).meta).toBe("can send · until 22 Nov");
  });
});

describe("the composer, from the mirrored level and what the owner answered", () => {
  const base = {
    status: "live" as const,
    peer: "gpu-box",
    level: "suggest" as string | null,
    offered: "suggest" as const,
    expired: false,
    refused: false,
    held: false,
    hasText: false,
  };

  test("a terminal has no composer, whatever level its row claims or was offered before", () => {
    for (const level of ["view", "suggest", "send"])
      for (const offered of [null, "suggest", "send"] as const)
        for (const hasText of [false, true])
          expect(
            composerState({ ...base, kind: "terminal", level, offered, hasText }),
            `${level}/${offered}/${hasText}`,
          ).toEqual({ kind: null, enabled: false, placeholder: "", button: "", note: null });
    // an API row (or one from before kinds) is unchanged
    expect(composerState({ ...base, kind: "api" }).kind).toBe("suggest");
    expect(composerState({ ...base, kind: null }).kind).toBe("suggest");
  });

  test("suggest and send each have their own composer; view has none", () => {
    expect(composerState(base)).toEqual({
      kind: "suggest",
      enabled: true,
      placeholder: "Suggest a message to gpu-box's operator…",
      button: "Suggest",
      note: null,
    });
    expect(composerState({ ...base, level: "send", offered: "send" })).toEqual({
      kind: "send",
      enabled: true,
      placeholder: "Message gpu-box's agent…",
      button: "Send",
      note: null,
    });
    expect(composerState({ ...base, level: "view", offered: null }).kind).toBeNull();
    expect(composerState({ ...base, level: "steer", offered: null }).kind).toBeNull();
    expect(composerState({ ...base, level: null, offered: null }).kind).toBeNull();
  });

  test("held: disabled, with the mission line", () => {
    const c = composerState({ ...base, held: true, hasText: true });
    expect(c).toMatchObject({
      kind: "suggest",
      enabled: false,
      note: "gpu-box's session is run by a mission right now. You can watch.",
    });
  });

  test("a level that ended keeps the composer, disabled, while there is text to keep", () => {
    for (const ended of [
      { level: "view" }, // lowered
      { expired: true }, // expired on this clock
      { refused: true }, // the owner refused a write under this revision
      { level: null }, // no longer listed
      { status: "gone" as const },
    ]) {
      const c = composerState({ ...base, level: "send", offered: "send", hasText: true, ...ended });
      expect(c).toMatchObject({
        kind: "send",
        enabled: false,
        note: "Send access ended. Your text is kept here; ask for access again.",
      });
      // with nothing typed there is nothing to keep: the pane is view only
      expect(
        composerState({ ...base, level: "send", offered: "send", hasText: false, ...ended }).kind,
      ).toBeNull();
    }
    expect(
      composerState({ ...base, level: "view", offered: "suggest", hasText: true }).note,
    ).toBe("Suggest access ended. Your text is kept here; ask for access again.");
  });

  test("send lowered to suggest is the Suggest composer; offline disables without a note", () => {
    const lowered = composerState({ ...base, level: "suggest", offered: "send", hasText: true });
    expect(lowered).toMatchObject({ kind: "suggest", enabled: true, note: null });
    const offline = composerState({ ...base, status: "offline", hasText: true });
    expect(offline).toMatchObject({ kind: "suggest", enabled: false, note: null });
    expect(composerState({ ...base, status: "loading" }).enabled).toBe(false);
  });
});

describe("a delivery state is what the owner answered, one presentation each", () => {
  test("only sent, queued and not_sent are what they say", () => {
    expect(deliveryOf("sent")).toBe("sent");
    expect(deliveryOf("queued")).toBe("queued");
    expect(deliveryOf("not_sent")).toBe("not_sent");
    for (const h of ["uncertain", null, undefined, "delivered", 7, {}])
      expect(deliveryOf(h)).toBe("unconfirmed");
  });

  test("sent is green; not sent can be sent again; not confirmed never can", () => {
    expect(deliveryLine("sent")).toEqual({ word: "sent", tone: "ok", detail: null, resend: false });
    expect(deliveryLine("queued")).toEqual({ word: "queued", tone: "idle", detail: null, resend: false });
    expect(deliveryLine("not_sent")).toEqual({
      word: "not sent",
      tone: "idle",
      detail: "It did not reach the agent.",
      resend: true,
    });
    expect(deliveryLine("unconfirmed")).toEqual({
      word: "delivery not confirmed",
      tone: "idle",
      detail: "It may still arrive. Do not resend blindly.",
      resend: false,
    });
    expect(deliveryLine("sending").resend).toBe(false);
  });

  test("a failed write: only an answer that says so means nothing was accepted", () => {
    // The whole table (#2061): status and code → what it means, and whether the record goes.
    const settled: [number, unknown, string][] = [
      [404, "no_grant", "refused"],
      [404, "not_found", "rejected"],
      [409, "held", "held"],
      [409, "conflict", "rejected"],
      [409, "not_pending", "rejected"],
      [413, "too_large", "rejected"],
      [422, "invalid", "rejected"],
      [422, "not_possible", "rejected"],
      [503, "peer_offline", "offline"], // no channel: the request never left this instance
      [401, undefined, "rejected"], // this instance turned the request away itself
      [403, "owner_only", "rejected"],
      [403, undefined, "rejected"],
    ];
    for (const [status, code, means] of settled) {
      expect(writeFailure(status, code), `${status} ${String(code)}`).toBe(means);
      expect(settlesRecord(writeFailure(status, code)), `${status} ${String(code)}`).toBe(true);
    }
    const ambiguous: [number, unknown][] = [
      [504, "peer_lost"], // the channel ended after the request was handed to it
      [504, "timeout"],
      // usually a refusal, but an owner before `answer_lost` said it AFTER delivering: never settled
      [429, "busy"],
      [503, "unavailable"], // the owner could not say: its submit may have run
      [503, "busy"],
      [503, "store"],
      [503, undefined], // a 503 that does not say the request never left
      [502, "bad_answer"],
      [500, undefined],
      [0, undefined], // no answer at all
      [408, undefined],
      // a status or a code this table does not know is never a refusal
      [404, undefined],
      [404, "something_new"],
      [409, undefined],
      [409, "something_new"],
      [422, undefined],
      [429, undefined],
      [429, "something_new"],
      [400, "invalid"],
      [418, "teapot"],
      [503, "peer_lost"],
    ];
    for (const [status, code] of ambiguous) {
      expect(writeFailure(status, code), `${status} ${String(code)}`).toBe("unconfirmed");
      expect(settlesRecord(writeFailure(status, code)), `${status} ${String(code)}`).toBe(false);
    }
  });

  test("a dropped channel is not proof of a refusal: the reviewer's probe", () => {
    // What a receiving write answered for a reply lost with the channel before #2061's fix
    // (`503`), and what it answers now (`504 peer_lost`): neither settles the record.
    expect(settlesRecord(writeFailure(503, undefined))).toBe(false);
    expect(settlesRecord(writeFailure(504, "peer_lost"))).toBe(false);
    // Only the code that says "no channel, never sent" does.
    expect(settlesRecord(writeFailure(503, "peer_offline"))).toBe(true);
  });
});

describe("your suggestions: each state its own words", () => {
  test("waiting is grey with Withdraw; approving is not approved; expired is not rejected", () => {
    expect(ownSuggestionLine("pending", "gpu-box")).toEqual({
      word: "waiting for gpu-box's operator",
      tone: "idle",
      withdraw: true,
    });
    expect(ownSuggestionLine("approving", "gpu-box")).toEqual({
      word: "approved · being delivered",
      tone: "idle",
      withdraw: false,
    });
    expect(ownSuggestionLine("approved", "gpu-box")).toEqual({ word: "approved", tone: "ok", withdraw: false });
    expect(ownSuggestionLine("rejected", "gpu-box").word).toBe("rejected");
    expect(ownSuggestionLine("withdrawn", "gpu-box").word).toBe("withdrawn");
    expect(ownSuggestionLine("expired", "gpu-box").word).toBe("expired");
    const words = ["pending", "approving", "approved", "rejected", "withdrawn", "expired"].map(
      (s) => ownSuggestionLine(s, "p").word,
    );
    expect(new Set(words).size).toBe(6);
  });
});

describe("one client key per composed message", () => {
  let n = 0;
  const mint = () => `key-${++n}`;

  test("a retry of the same text keeps its key; other text always gets a new one", () => {
    const first = draftKey(null, "hello", mint);
    expect(draftKey(first, "hello", mint)).toBe(first); // the same message, retried
    const edited = draftKey(first, "hello!", mint);
    expect(edited.key).not.toBe(first.key); // different text: never the old key
    // after a message was accepted the composer holds none: the next one is new, same text or not
    expect(draftKey(null, "hello", mint).key).not.toBe(first.key);
  });
});

describe("the in-flight record holds no content", () => {
  const store = () => {
    const data = new Map<string, string>();
    return {
      data,
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
    };
  };
  const op = (over: Partial<PendingOp> = {}): PendingOp => ({
    peer_id: GPU,
    grant_id: GRANT,
    client_key: "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d",
    kind: "send",
    started_at: 1700000000000,
    ...over,
  });

  test("exactly five fields are written, whatever the caller hands over", () => {
    const s = store();
    pendingOps.add({ ...op(), text: "SECRET TEXT", title: "a title" } as PendingOp, s);
    const raw = [...s.data.values()].join("");
    expect(raw).not.toContain("SECRET TEXT");
    expect(raw).not.toContain("a title");
    expect(JSON.parse(raw)).toEqual([op()]);
    expect(Object.keys(JSON.parse(raw)[0]).sort()).toEqual(
      ["client_key", "grant_id", "kind", "peer_id", "started_at"].sort(),
    );
  });

  test("records are per share, removed by key, and anything malformed is not read back", () => {
    const s = store();
    pendingOps.add(op(), s);
    pendingOps.add(op({ client_key: "1b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d", kind: "suggest" }), s);
    pendingOps.add(op({ client_key: "2b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d", grant_id: "h".repeat(22) }), s);
    expect(pendingOps.of(GPU, GRANT, s).map((o) => o.kind)).toEqual(["send", "suggest"]);
    pendingOps.remove("0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d", s);
    expect(pendingOps.of(GPU, GRANT, s)).toHaveLength(1);
    pendingOps.add(op({ client_key: "not-a-uuid" }), s);
    pendingOps.add(op({ kind: "delete" as "send" }), s);
    expect(pendingOps.all(s)).toHaveLength(2);
    s.data.set("bl-remote-ops", "{not json");
    expect(pendingOps.all(s)).toEqual([]);
    s.data.set("bl-remote-ops", JSON.stringify([{ ...op(), text: "smuggled" }, "x", null]));
    expect(pendingOps.all(s)).toEqual([op()]);
  });
});

describe("the generation fence covers every answer to the pane's own requests", () => {
  const TARGET = `${GPU}/${GRANT}`;
  const KEY = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d";
  const SID = "s".repeat(22);
  const snapOf = (ids: string[], over: Partial<RemoteSnapshot> = {}): RemoteSnapshot => ({
    link: "L",
    revision: 1,
    event_cursor: 1,
    turns: ids.map((turn_id) => ({ turn_id })),
    omitted_turns: 0,
    pending_requests: [],
    can_send: false,
    ...over,
  });
  const liveState = (leftovers?: PendingOp[]) =>
    remotePaneReduce(remotePaneInit(TARGET, leftovers), {
      type: "snapshot",
      generation: 0,
      snapshot: snapOf(["a"]),
    });
  const row = (state: string) => ({
    suggestion_id: SID,
    client_key: KEY,
    state,
    handoff: null,
    created_at: 10,
    updated_at: 11,
  });
  /** Every answer this pane can receive to a request of its own, under generation 0. */
  const answers = [
    { type: "suggestions", generation: 0, rows: [row("pending")] },
    { type: "suggested", generation: 0, client_key: KEY, suggestion: { suggestion_id: SID, state: "pending" }, now: 1 },
    { type: "withdrawn", generation: 0, suggestion: { suggestion_id: SID, state: "withdrawn" }, now: 1 },
    { type: "sending", generation: 0, client_key: KEY, text: "hello" },
    { type: "sent", generation: 0, client_key: KEY, handoff: "sent" },
    { type: "unconfirmed", generation: 0, client_key: KEY },
    { type: "unsent", generation: 0, client_key: KEY },
    { type: "held", generation: 0, held: true },
    { type: "refused", generation: 0, revision: 3 },
    { type: "checking", generation: 0, client_key: KEY },
    { type: "status", generation: 0, client_key: KEY, result: { known: true, kind: "send", handoff: "sent" }, now: 1 },
    { type: "giveup", generation: 0, client_key: KEY },
    { type: "history", generation: 0, page: { link: "L", turns: [{ turn_id: "z" }], before: null, has_more: false } },
  ] as const;

  test.each([
    ["access was lost (404)", { type: "failed", generation: 0, status: 404 } as const],
    ["the peer went offline (503)", { type: "failed", generation: 0, status: 503 } as const],
    ["another shared session was opened", { type: "open", target: `${GPU}/${"h".repeat(22)}` } as const],
  ])("after %s, no late answer changes anything", (_name, clear) => {
    const leftover: PendingOp = { peer_id: GPU, grant_id: GRANT, client_key: KEY, kind: "send", started_at: 1 };
    const cleared = remotePaneReduce(liveState([leftover]), clear);
    expect(cleared.generation).toBe(1);
    for (const late of answers) expect(remotePaneReduce(cleared, late), late.type).toBe(cleared);
    expect(cleared.suggestions).toBeNull();
    expect(cleared.outbox).toEqual([]);
    expect(cleared.held).toBe(false);
    expect(cleared.refusedRevision).toBeNull();
  });

  test("a clear drops what the owner answered and keeps this browser's own leftover keys", () => {
    let s = liveState([{ peer_id: GPU, grant_id: GRANT, client_key: KEY, kind: "send", started_at: 1 }]);
    s = remotePaneReduce(s, { type: "suggestions", generation: 0, rows: [row("pending")] });
    s = remotePaneReduce(s, { type: "sending", generation: 0, client_key: "k2", text: "hi" });
    s = remotePaneReduce(s, { type: "held", generation: 0, held: true });
    s = remotePaneReduce(s, { type: "checking", generation: 0, client_key: KEY });
    const offline = remotePaneReduce(s, { type: "failed", generation: 0, status: 503 });
    expect(offline.suggestions).toBeNull();
    expect(offline.outbox).toEqual([]);
    expect(offline.held).toBe(false);
    // the check that was in flight is asked again under the new generation
    expect(offline.leftovers).toMatchObject([{ client_key: KEY, phase: "pending" }]);
    // another target brings its own keys and none of this one's
    const other = remotePaneReduce(s, {
      type: "open",
      target: "x/y",
      leftovers: [{ peer_id: "x", grant_id: "y", client_key: "other", kind: "suggest", started_at: 2 }],
    });
    expect(other.leftovers.map((l) => l.client_key)).toEqual(["other"]);
    expect(remotePaneReduce(s, { type: "open", target: "x/y" }).leftovers).toEqual([]);
  });

  test("under the current generation: the list, a suggestion, its withdrawal", () => {
    let s = liveState();
    s = remotePaneReduce(s, {
      type: "suggestions",
      generation: 0,
      rows: [row("approving"), { ...row("weird"), suggestion_id: "t".repeat(22) }, "x", { ...row("pending") }],
    });
    // unknown states and repeated ids are not listed
    expect(s.suggestions).toEqual([row("approving")]);
    s = remotePaneReduce(s, {
      type: "suggested",
      generation: 0,
      client_key: "k9",
      suggestion: { suggestion_id: "n".repeat(22), state: "pending" },
      now: 5000,
    });
    expect(s.suggestions?.[0]).toMatchObject({ suggestion_id: "n".repeat(22), client_key: "k9", state: "pending" });
    s = remotePaneReduce(s, {
      type: "withdrawn",
      generation: 0,
      suggestion: { suggestion_id: "n".repeat(22), state: "withdrawn" },
      now: 6000,
    });
    expect(s.suggestions?.[0].state).toBe("withdrawn");
    // an answer that is not a suggestion changes nothing
    expect(remotePaneReduce(s, { type: "suggested", generation: 0, client_key: "k", suggestion: { state: "pending" }, now: 1 })).toBe(s);
  });

  test("a sent message: sending, the owner's receipt, and the turn it became", () => {
    let s = remotePaneReduce(remotePaneInit(TARGET), {
      type: "snapshot",
      generation: 0,
      snapshot: snapOf([], { turns: [{ turn_id: "old", mine: true, text: "hello" }] }),
    });
    s = remotePaneReduce(s, { type: "sending", generation: 0, client_key: KEY, text: "hello" });
    expect(s.outbox).toEqual([{ client_key: KEY, text: "hello", delivery: "sending", prior: ["old"], turn: null }]);
    s = remotePaneReduce(s, { type: "sent", generation: 0, client_key: KEY, handoff: null });
    expect(s.outbox[0]).toMatchObject({ delivery: "unconfirmed", turn: null }); // no receipt is not "sent"
    s = remotePaneReduce(s, { type: "sent", generation: 0, client_key: KEY, handoff: "queued" });
    expect(s.outbox[0].delivery).toBe("queued");
    // the conversation then shows OUR new turn with that text: the row is tied to it, not to the
    // older turn with the same words, and not to someone else's
    s = remotePaneReduce(s, {
      type: "snapshot",
      generation: 0,
      snapshot: snapOf([], {
        revision: 2,
        turns: [
          { turn_id: "old", mine: true, text: "hello" },
          { turn_id: "theirs", mine: false, text: "hello" },
          { turn_id: "new", mine: true, text: "hello" },
        ],
      }),
    });
    expect(s.outbox[0].turn).toBe("new");
    // a definite refusal removes the row (the text is still in the composer)
    s = remotePaneReduce(s, { type: "sending", generation: 0, client_key: "k2", text: "more" });
    s = remotePaneReduce(s, { type: "unsent", generation: 0, client_key: "k2" });
    expect(s.outbox.map((r) => r.client_key)).toEqual([KEY]);
  });

  test("not sent: one row tied to its recorded turn, whichever came first, the reply or the snapshot", () => {
    // The owner's `not_sent` is a RECORDED turn the agent never got (the runtime answers the
    // receipt from that turn), so the conversation shows it. The row belongs under it in both
    // orders, with the same guarded resend (`deliveryLine(...).resend`).
    const live = remotePaneReduce(remotePaneInit(TARGET), { type: "snapshot", generation: 0, snapshot: snapOf([]) });
    const sending = remotePaneReduce(live, { type: "sending", generation: 0, client_key: KEY, text: "run it" });
    const shown = {
      type: "snapshot",
      generation: 0,
      snapshot: snapOf([], { revision: 2, turns: [{ turn_id: "turn", mine: true, text: "run it" }] }),
    } as const;
    const reply = { type: "sent", generation: 0, client_key: KEY, handoff: "not_sent" } as const;
    const row = { client_key: KEY, text: "run it", delivery: "not_sent", prior: [], turn: "turn" };
    const replyFirst = remotePaneReduce(remotePaneReduce(sending, reply), shown);
    const snapshotFirst = remotePaneReduce(remotePaneReduce(sending, shown), reply);
    expect(replyFirst.outbox).toEqual([row]);
    expect(snapshotFirst.outbox).toEqual([row]);
    expect(deliveryLine(row.delivery as "not_sent").resend).toBe(true);

    // Send again is the SAME row under the SAME key: never a second row, and it stays on its turn.
    let s = remotePaneReduce(snapshotFirst, { type: "sending", generation: 0, client_key: KEY, text: "run it" });
    expect(s.outbox).toEqual([{ ...row, delivery: "sending" }]);
    s = remotePaneReduce(s, { type: "sent", generation: 0, client_key: KEY, handoff: "not_sent" });
    expect(s.outbox).toEqual([row]);

    // The same words typed again are another message (another key) and another turn: it gets
    // the new turn, and the not-sent row keeps its own.
    s = remotePaneReduce(s, { type: "sending", generation: 0, client_key: "k2", text: "run it" });
    expect(s.outbox[1]).toMatchObject({ client_key: "k2", prior: ["turn"], turn: null });
    s = remotePaneReduce(s, {
      type: "snapshot",
      generation: 0,
      snapshot: snapOf([], {
        revision: 3,
        turns: [
          { turn_id: "turn", mine: true, text: "run it" },
          { turn_id: "turn2", mine: true, text: "run it" },
        ],
      }),
    });
    expect(s.outbox.map((r) => [r.client_key, r.turn])).toEqual([
      [KEY, "turn"],
      ["k2", "turn2"],
    ]);
  });
});

describe("a reload in the middle of a request: find out, never assume", () => {
  const TARGET = `${GPU}/${GRANT}`;
  const KEY = "0b9e3c1a-7f52-4c1d-9a6e-3d2f1b0a9c8d";
  const op = (kind: "send" | "suggest" = "send"): PendingOp => ({
    peer_id: GPU,
    grant_id: GRANT,
    client_key: KEY,
    kind,
    started_at: 1,
  });
  const start = (kind: "send" | "suggest" = "send") =>
    remotePaneReduce(remotePaneInit(TARGET, [op(kind)]), {
      type: "snapshot",
      generation: 0,
      snapshot: { link: "L", revision: 1, event_cursor: 1, turns: [], omitted_turns: 0, pending_requests: [], can_send: false },
    });
  const status = (s: RemotePaneState, result: unknown, now: number) =>
    remotePaneReduce(s, { type: "status", generation: 0, client_key: KEY, result: result as never, now });

  test("a leftover is asked about at once", () => {
    expect(reconcilePlan(start().leftovers, 0)).toEqual({ check: [KEY], giveUp: [], wakeIn: null });
    expect(reloadNotices(start().leftovers, "live")).toEqual([]); // nothing is claimed before an answer
  });

  test("known: the real state, and the notice is gone", () => {
    const sent = status(start(), { known: true, kind: "send", handoff: "queued" }, 1000);
    expect(sent.leftovers).toEqual([]);
    expect(sent.outbox).toEqual([{ client_key: KEY, text: null, delivery: "queued", prior: [], turn: null }]);
    const suggested = status(start("suggest"), { known: true, kind: "suggest", suggestion_id: "s".repeat(22), state: "pending", handoff: null }, 1000);
    expect(suggested.leftovers).toEqual([]);
    expect(suggested.outbox).toEqual([]); // it is in the list the owner answers
  });

  test("unknown is 'may or may not have arrived': asked again at 5 s and 30 s, given up at 2 min", () => {
    let s = status(start(), { known: false }, 1000);
    expect(s.leftovers[0]).toMatchObject({ phase: "unknown", firstUnknownAt: 1000, nextAt: 6000 });
    expect(reloadNotices(s.leftovers, "live")).toEqual([RELOAD_UNKNOWN]);
    expect(RELOAD_UNKNOWN).toContain("may or may not have arrived");
    expect(RELOAD_UNKNOWN).not.toMatch(/did not arrive|not sent|failed/i);
    expect(reconcilePlan(s.leftovers, 1000)).toEqual({ check: [], giveUp: [], wakeIn: 5000 });
    expect(reconcilePlan(s.leftovers, 6000).check).toEqual([KEY]);
    s = remotePaneReduce(s, { type: "checking", generation: 0, client_key: KEY });
    expect(reconcilePlan(s.leftovers, 6000)).toEqual({ check: [], giveUp: [], wakeIn: null });
    s = status(s, { known: false }, 6100);
    expect(s.leftovers[0]).toMatchObject({ phase: "unknown", firstUnknownAt: 1000, nextAt: 31000 });
    s = status(remotePaneReduce(s, { type: "checking", generation: 0, client_key: KEY }), { known: false }, 31050);
    expect(s.leftovers[0].nextAt).toBeNull(); // no further scheduled check
    expect(reconcilePlan(s.leftovers, 31050)).toEqual({ check: [], giveUp: [], wakeIn: RECONCILE_GIVE_UP_MS - 30050 });
    expect(reconcilePlan(s.leftovers, 1000 + RECONCILE_GIVE_UP_MS).giveUp).toEqual([KEY]);
    s = remotePaneReduce(s, { type: "giveup", generation: 0, client_key: KEY });
    // the wording does not change when it gives up, and nothing more is asked
    expect(reloadNotices(s.leftovers, "live")).toEqual([RELOAD_UNKNOWN]);
    expect(reconcilePlan(s.leftovers, 9e9)).toEqual({ check: [], giveUp: [], wakeIn: null });
    expect(status(s, { known: true, kind: "send", handoff: "sent" }, 9e9)).toBe(s);
  });

  test("the delayed original: unknown first, admitted later, the notice becomes the real state", () => {
    let s = status(start(), { known: false }, 1000);
    expect(reloadNotices(s.leftovers, "live")).toEqual([RELOAD_UNKNOWN]);
    s = remotePaneReduce(s, { type: "checking", generation: 0, client_key: KEY });
    // asking again does not take the notice away: it stays until an answer says what happened
    expect(reloadNotices(s.leftovers, "live")).toEqual([RELOAD_UNKNOWN]);
    s = status(s, { known: true, kind: "send", handoff: "sent" }, 6000);
    expect(reloadNotices(s.leftovers, "live")).toEqual([]);
    expect(s.outbox).toMatchObject([{ client_key: KEY, delivery: "sent", text: null }]);
  });

  test("a failed status read is 'could not check', kept and retried, never absence", () => {
    let s = status(start(), "failed", 1000);
    expect(s.leftovers[0]).toMatchObject({ phase: "failed", nextAt: 6000 });
    expect(reloadNotices(s.leftovers, "live")).toEqual([RELOAD_UNCHECKED]);
    expect(reconcilePlan(s.leftovers, 2000)).toEqual({ check: [], giveUp: [], wakeIn: 4000 });
    expect(reconcilePlan(s.leftovers, 6000).check).toEqual([KEY]);
    expect(reconcilePlan(s.leftovers, 1000 + 10 * RECONCILE_GIVE_UP_MS).giveUp).toEqual([]); // never given up
    // the peer went offline: the key stays and is asked about again when it is back
    s = remotePaneReduce(s, { type: "failed", generation: 0, status: 503 });
    expect(s.leftovers).toMatchObject([{ client_key: KEY, phase: "pending" }]);
    // a share that is gone for good can no longer be asked: it could not be checked
    const gone = remotePaneReduce(start(), { type: "failed", generation: 0, status: 404 });
    expect(reloadNotices(gone.leftovers, "gone")).toEqual([RELOAD_UNCHECKED]);
  });
});

describe("a turn a peer caused carries its attribution through the rebuild", () => {
  test("the owner's name for the peer and the edited flag, and nothing else of the author", () => {
    const view = remoteTurnView(
      turn("t1", { author: { name: "studio", via: "peer", actor: "Robin", edited: true }, mine: true }),
    );
    expect(view.author).toEqual({ name: "studio", via: "peer", edited: true });
    expect(view.mine).toBe(true);
    expect(remoteTurnView(turn("t2", { author: { name: "studio", via: "peer", edited: "yes" } })).author).toEqual({
      name: "studio",
      via: "peer",
    });
    expect(remoteTurnView(turn("t3", { author: { name: 5, via: "peer" } })).author).toBeNull();
    expect(remoteTurnView(turn("t4", { author: { name: "x", via: "admin" } })).author).toBeNull();
  });
});
