/** A shared terminal's screen (#2060): the frame loop's reducer, with the clock as an argument.
 *  Every row of the issue's state table is a test here, and so are the 30 s bound, its reset of
 *  `have`, and the fence. `loop` drives the reducer exactly as the pane does: it asks with
 *  `screenHave` of the state the last answer left, so the fingerprint hand-off is what is
 *  tested, not a description of it. */
import { describe, expect, test } from "vitest";
import {
  REMOTE_RETRY_MS,
  REMOTE_STALL_LIMIT,
  TERMINAL_LIMIT_DETAIL,
  TERMINAL_LIMIT_LEAD,
  TERMINAL_LIMIT_UNPROTECTED,
  TERMINAL_NOT_SHOWN,
  TERMINAL_VIEW_ONLY,
  remoteRowState,
  shareKind,
  type RemoteShare,
} from "./federation";
import {
  REMOTE_FRAME_WAIT_S,
  SCREEN_NOT_A_FRAME,
  SCREEN_STALE_MS,
  SCREEN_UNAVAILABLE,
  SCREEN_UNAVAILABLE_DETAIL,
  screenBadge,
  screenDeadline,
  screenFailure,
  screenFrame,
  screenHave,
  screenInit,
  screenLines,
  screenReduce,
  screenRuns,
  type RemoteScreenFrame,
  type ScreenState,
} from "./federationScreen";

const TARGET = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl/g7Jx9aBcDeFgHiJkLmNoPq";
const FP_A = "a".repeat(32);
const FP_B = "b".repeat(32);
const T0 = 1_000_000;

const frame = (over: Partial<RemoteScreenFrame> = {}): RemoteScreenFrame => ({
  fingerprint: FP_A,
  cols: 20,
  rows: 3,
  lines: ["$ make deploy", "", "TOKEN=████████"],
  settled: true,
  ...over,
});

const answer = (s: ScreenState, body: unknown, at = T0) =>
  screenReduce(s, { type: "answer", generation: s.generation, answer: body, at });
const failed = (s: ScreenState, status: number, code: string | null = null, at = T0) =>
  screenReduce(s, { type: "failed", generation: s.generation, status, code, at });
const tick = (s: ScreenState, at: number) => screenReduce(s, { type: "tick", at });
const shown = (s: ScreenState) => ({
  frame: s.frame?.fingerprint ?? null,
  updating: s.updating,
  dimmed: s.dimmed,
  status: s.status,
});

/** An owner as far as `have` goes: it answers `unchanged` to the fingerprint of its current
 *  frame, and the frame to anything else. */
const owner = (current: RemoteScreenFrame) => (have: string | null) =>
  have === current.fingerprint ? { fingerprint: have, unchanged: true as const } : current;

describe("the frame loop: each answer of the table", () => {
  test("the first read sends no `have`, and the wait is 3 s (under Home Free's 5 s cut)", () => {
    expect(screenHave(screenInit(TARGET))).toBeNull();
    expect(REMOTE_FRAME_WAIT_S).toBe(3);
    expect(shown(screenInit(TARGET))).toEqual({
      frame: null,
      updating: false,
      dimmed: false,
      status: "loading",
    });
  });

  test("a settled frame replaces whatever was shown, with no marker", () => {
    let s = answer(screenInit(TARGET), frame());
    expect(shown(s)).toEqual({ frame: FP_A, updating: false, dimmed: false, status: "live" });
    expect(screenHave(s)).toBe(FP_A); // the fingerprint handed to the next read
    s = answer(s, frame({ fingerprint: FP_B, lines: ["$ ls"] }));
    expect(shown(s)).toEqual({ frame: FP_B, updating: false, dimmed: false, status: "live" });
    expect(s.frame?.lines).toEqual(["$ ls"]);
    expect(screenHave(s)).toBe(FP_B);
    expect(screenDeadline(s)).toBeNull();
  });

  test("an unsettled frame is shown at full strength, marked updating…", () => {
    // After a dimmed one, to prove it REPLACES and undims: it is fresh.
    let s = answer(answer(screenInit(TARGET), frame()), { settling: true });
    expect(s.dimmed).toBe(true);
    s = answer(s, frame({ fingerprint: FP_B, settled: false }));
    expect(shown(s)).toEqual({ frame: FP_B, updating: true, dimmed: false, status: "live" });
    expect(screenHave(s)).toBe(FP_B);
    expect(screenDeadline(s)).toBeNull(); // no stale clock runs for a fresh frame
    // ...and it is replaced by the next frame like any other.
    s = answer(s, frame({ fingerprint: FP_A }));
    expect(shown(s)).toEqual({ frame: FP_A, updating: false, dimmed: false, status: "live" });
  });

  test("`unchanged` keeps the frame and clears the marker: the owner confirmed it is current", () => {
    const unsettled = answer(screenInit(TARGET), frame({ settled: false }));
    expect(unsettled.updating).toBe(true);
    const s = answer(unsettled, { fingerprint: FP_A, unchanged: true });
    expect(shown(s)).toEqual({ frame: FP_A, updating: false, dimmed: false, status: "live" });
    expect(s.frame).toBe(unsettled.frame); // the same frame, not a copy of an older answer

    // After `settling` too: the dim, the marker and the clock all go.
    const dimmed = answer(answer(screenInit(TARGET), frame()), { settling: true });
    const back = answer(dimmed, { fingerprint: FP_A, unchanged: true }, T0 + 5_000);
    expect(shown(back)).toEqual({ frame: FP_A, updating: false, dimmed: false, status: "live" });
    expect(screenDeadline(back)).toBeNull();
  });

  test("`unchanged` for a frame the pane does not show changes nothing", () => {
    const empty = answer(screenInit(TARGET), { settling: true });
    expect(answer(empty, { fingerprint: FP_A, unchanged: true })).toBe(empty);
    const has = answer(screenInit(TARGET), frame({ settled: false }));
    expect(answer(has, { fingerprint: FP_B, unchanged: true })).toBe(has); // the marker stays
  });

  test("`settling` dims the last frame under updating…; with no frame, updating… alone", () => {
    const alone = answer(screenInit(TARGET), { settling: true });
    expect(shown(alone)).toEqual({ frame: null, updating: true, dimmed: false, status: "live" });
    expect(screenDeadline(alone)).toBeNull(); // nothing on screen: no clock
    expect(screenHave(alone)).toBeNull();

    const s = answer(answer(screenInit(TARGET), frame()), { settling: true }, T0 + 10);
    expect(shown(s)).toEqual({ frame: FP_A, updating: true, dimmed: true, status: "live" });
    expect(screenHave(s)).toBe(FP_A); // still the frame on screen
    expect(screenDeadline(s)).toBe(T0 + 10 + SCREEN_STALE_MS);
  });

  test("503 unavailable: the frame is dropped and the state says the screen can't be shown", () => {
    const had = answer(screenInit(TARGET), frame());
    const s = failed(had, 503, "unavailable");
    expect(shown(s)).toEqual({ frame: null, updating: false, dimmed: false, status: "unavailable" });
    expect(s.reason).toBe("screen"); // the owner's refusal: the pane says what is never sent
    expect(s.generation).toBe(had.generation + 1);
    expect(s.startAfterMs).toBe(REMOTE_RETRY_MS); // asked again, without a tight loop
    expect(screenHave(s)).toBeNull();
    expect(screenBadge(s)).toEqual({ led: "up", word: "connected" }); // the peer IS connected
    expect(SCREEN_UNAVAILABLE).toBe("This screen can't be shown right now.");
    expect(SCREEN_UNAVAILABLE_DETAIL).toBe("It will appear when the screen can be read again.");
    // It fills again from the next answer.
    expect(answer(s, frame())).toMatchObject({ status: "live", reason: null });
    expect(shown(answer(s, frame()))).toMatchObject({ frame: FP_A, status: "live" });
  });

  test("503 peer_offline: the frame is dropped and the peer is said to be offline", () => {
    const had = answer(screenInit(TARGET), frame());
    const s = failed(had, 503, "peer_offline");
    expect(shown(s)).toEqual({ frame: null, updating: false, dimmed: false, status: "offline" });
    expect(s.generation).toBe(had.generation + 1);
    expect(s.startAfterMs).toBe(REMOTE_RETRY_MS);
    expect(screenHave(s)).toBeNull();
    expect(screenBadge(s)).toEqual({ led: "idle", word: "offline" });
  });

  test("404: the frame is dropped for good and nothing is asked again", () => {
    const had = answer(screenInit(TARGET), frame());
    const s = failed(had, 404, "no_grant");
    expect(shown(s)).toEqual({ frame: null, updating: false, dimmed: false, status: "gone" });
    expect(s.startAfterMs).toBeNull();
    expect(s.generation).toBe(had.generation + 1);
    expect(screenBadge(s)).toEqual({ led: "idle", word: "unavailable" });
    // Final: an answer under the old generation cannot bring it back.
    expect(screenReduce(s, { type: "answer", generation: had.generation, answer: frame(), at: T0 })).toBe(s);
  });

  test("the two 503s are told apart by the server's code, never guessed", () => {
    expect(screenFailure(503, "unavailable")).toBe("unavailable");
    expect(screenFailure(503, "peer_offline")).toBe("offline");
    expect(screenFailure(404, null)).toBe("gone");
    for (const [status, code] of [
      [503, null],
      [503, "something_else"],
      [0, null],
      [429, "busy"],
      [504, "timeout"],
      [502, "bad_answer"],
    ] as const)
      expect(screenFailure(status, code)).toBe("stall");
  });
});

describe("the 30 second bound", () => {
  const dimmedAt = (at: number) => answer(answer(screenInit(TARGET), frame()), { settling: true }, at);

  test("the clock starts at the first `settling` with a frame shown, and later ones do not move it", () => {
    let s = dimmedAt(T0);
    s = answer(s, { settling: true }, T0 + 9_000);
    s = answer(s, { settling: true }, T0 + 18_000);
    expect(screenDeadline(s)).toBe(T0 + SCREEN_STALE_MS);
    expect(tick(s, T0 + SCREEN_STALE_MS - 1)).toBe(s); // not yet
  });

  test("at 30 s the frame is dropped, updating… stays alone, and `have` is dropped with it", () => {
    const s = dimmedAt(T0);
    expect(screenHave(s)).toBe(FP_A);
    const dropped = tick(s, T0 + SCREEN_STALE_MS);
    expect(shown(dropped)).toEqual({ frame: null, updating: true, dimmed: false, status: "live" });
    expect(screenHave(dropped)).toBeNull(); // nothing on screen: the next read sends none
    expect(dropped.generation).toBe(s.generation + 1); // the read still asking with FP_A is cut
    expect(dropped.startAfterMs).toBe(0); // and the next one is asked at once
    expect(screenDeadline(dropped)).toBeNull();
  });

  test("an empty pane cannot stay empty: the owner would answer `unchanged` to the old fingerprint", () => {
    const current = frame();
    const ask = owner(current);
    const dropped = tick(dimmedAt(T0), T0 + SCREEN_STALE_MS);
    // The pane asks with what is on screen, which is nothing, so the owner sends the frame.
    const refilled = answer(dropped, ask(screenHave(dropped)), T0 + SCREEN_STALE_MS + 50);
    expect(shown(refilled)).toEqual({ frame: FP_A, updating: false, dimmed: false, status: "live" });
    // Had the old fingerprint been sent, the answer would be `unchanged`, and it fills nothing.
    expect(shown(answer(dropped, ask(FP_A)))).toMatchObject({ frame: null });
  });

  test("a read that was in flight when the bound fired is discarded, whatever it answers", () => {
    const s = dimmedAt(T0);
    const dropped = tick(s, T0 + SCREEN_STALE_MS);
    for (const body of [{ fingerprint: FP_A, unchanged: true }, frame(), { settling: true }])
      expect(screenReduce(dropped, { type: "answer", generation: s.generation, answer: body, at: T0 })).toBe(dropped);
  });

  test("any frame, and `unchanged`, clear the clock", () => {
    const s = dimmedAt(T0);
    for (const body of [frame({ fingerprint: FP_B }), frame({ fingerprint: FP_B, settled: false }), { fingerprint: FP_A, unchanged: true }]) {
      const next = answer(s, body, T0 + 20_000);
      expect(screenDeadline(next)).toBeNull();
      expect(tick(next, T0 + 10 * SCREEN_STALE_MS)).toBe(next); // nothing is ever dropped later
    }
    // A new `settling` after that starts a NEW clock from its own time.
    const again = answer(answer(s, frame({ fingerprint: FP_B }), T0 + 20_000), { settling: true }, T0 + 25_000);
    expect(screenDeadline(again)).toBe(T0 + 25_000 + SCREEN_STALE_MS);
  });

  test("a tick with no clock running changes nothing", () => {
    for (const s of [screenInit(TARGET), answer(screenInit(TARGET), frame()), answer(screenInit(TARGET), { settling: true })])
      expect(tick(s, T0 + 10 * SCREEN_STALE_MS)).toBe(s);
  });
});

describe("the fence and what is never trusted", () => {
  test("an answer from another generation is discarded", () => {
    const s = answer(screenInit(TARGET), frame());
    const cleared = failed(s, 503, "peer_offline");
    const late = { type: "answer", generation: s.generation, answer: frame({ fingerprint: FP_B }), at: T0 } as const;
    expect(screenReduce(cleared, late)).toBe(cleared);
    expect(screenReduce(cleared, { type: "failed", generation: s.generation, status: 404, code: null, at: T0 })).toBe(cleared);
  });

  test("another peer or grant clears the frame and starts a new generation", () => {
    const s = answer(screenInit(TARGET), frame());
    expect(screenReduce(s, { type: "open", target: TARGET })).toBe(s);
    const other = screenReduce(s, { type: "open", target: "other/grant" });
    expect(other).toMatchObject({ target: "other/grant", generation: s.generation + 1, frame: null, status: "loading", startAfterMs: 0 });
    expect(screenHave(other)).toBeNull();
    // The old grant's slow frame never lands in the new pane.
    expect(screenReduce(other, { type: "answer", generation: s.generation, answer: frame(), at: T0 })).toBe(other);
  });

  test("a read that fails without a word about the screen dims the frame; three in a row drop it", () => {
    let s = answer(screenInit(TARGET), frame());
    s = failed(s, 504, "timeout", T0 + 100);
    expect(shown(s)).toEqual({ frame: FP_A, updating: true, dimmed: true, status: "live" });
    expect(screenBadge(s)).toEqual({ led: "idle", word: "reconnecting" });
    expect(screenDeadline(s)).toBe(T0 + 100 + SCREEN_STALE_MS); // the same bound applies
    // A good answer forgets the stalls.
    expect(answer(s, { fingerprint: FP_A, unchanged: true })).toMatchObject({ stalls: 0, dimmed: false, updating: false });
    for (let i = 1; i < REMOTE_STALL_LIMIT; i++) s = failed(s, 0, null, T0 + 200 * i);
    expect(shown(s)).toEqual({ frame: null, updating: false, dimmed: false, status: "unavailable" });
    expect(s.reason).toBe("read"); // nothing was said about the screen: no limit is claimed
    expect(screenBadge(s)).toEqual({ led: "idle", word: "reconnecting" }); // not "connected"
    expect(screenHave(s)).toBeNull();
  });

  test("only a well-formed frame is drawn", () => {
    expect(screenFrame(frame())).toEqual(frame());
    const bad: unknown[] = [
      null,
      "frame",
      [],
      {},
      frame({ fingerprint: "A".repeat(32) }),
      frame({ fingerprint: "a".repeat(31) }),
      frame({ cols: 0 }),
      frame({ cols: 501 }),
      frame({ cols: 2.5 }),
      frame({ rows: 0 }),
      frame({ rows: 301 }),
      frame({ lines: ["a", "b", "c", "d"] }), // more lines than rows
      frame({ lines: ["x".repeat(21)] }), // a line longer than cols
      { ...frame(), lines: [7] },
      { ...frame(), lines: "$ ls" },
      { ...frame(), settled: "yes" },
      { ...frame(), cols: "20" },
    ];
    const s = answer(screenInit(TARGET), frame());
    for (const body of bad) {
      expect(screenFrame(body)).toBeNull();
      // Not viewable: what was shown is dropped (with its `have`), nothing half-read is drawn,
      // and the pane says what arrived was not a screen it can draw.
      const next = answer(s, body);
      expect(shown(next)).toEqual({ frame: null, updating: false, dimmed: false, status: "unavailable" });
      expect(next.reason).toBe("frame");
      expect(screenHave(next)).toBeNull();
      expect(next.generation).toBe(s.generation + 1);
      expect(next.startAfterMs).toBe(REMOTE_RETRY_MS);
    }
    expect(SCREEN_NOT_A_FRAME).toBe("What arrived is not a screen this page can draw.");
    // A line is measured in characters, as the server measures it: an astral one counts once.
    expect(screenFrame(frame({ cols: 2, lines: ["😀█"] }))).not.toBeNull();
  });

  // #2055 review, the same class here: a number a peer sends is untrusted. `rows`, `cols` and the
  // lines size the block, pad it and bound every loop over it, so none of them may be used before
  // it is checked, and a frame that fails is "not viewable", never thrown on.
  const HOSTILE_NUMBERS: unknown[] = [1e300, -1e300, Number.MAX_VALUE, Infinity, -Infinity, NaN, -1, 0, 0.5, 1e6, 2 ** 31, "80", null, true, [80], { n: 80 }];
  test.each(HOSTILE_NUMBERS)("a frame whose `cols` is %s is not viewable", (cols) => {
    const body = { ...frame(), cols };
    expect(() => screenFrame(body)).not.toThrow();
    expect(screenFrame(body)).toBeNull();
    expect(answer(screenInit(TARGET), body)).toMatchObject({ status: "unavailable", reason: "frame", frame: null });
  });
  test.each(HOSTILE_NUMBERS)("a frame whose `rows` is %s is not viewable", (rows) => {
    const body = { ...frame(), rows };
    expect(() => screenFrame(body)).not.toThrow();
    expect(screenFrame(body)).toBeNull();
    expect(answer(screenInit(TARGET), body)).toMatchObject({ status: "unavailable", reason: "frame", frame: null });
  });

  test("hostile lines: too many, too long, not strings; none is measured past `cols`", () => {
    const bad: unknown[] = [
      Array.from({ length: 301 }, () => ""), // more than ROWS_MAX
      Array.from({ length: 4 }, () => ""), // more than this frame's rows (3)
      ["x".repeat(21)], // one past cols
      ["x".repeat(5_000_000)], // far past it: rejected without walking it
      ["😀".repeat(21)], // past cols in characters, not only in UTF-16 units
      [null],
      [undefined],
      [7],
      [{ toString: () => "ls" }],
      [["ls"]],
      { length: 3 },
      "$ ls",
    ];
    for (const lines of bad) {
      const body = { ...frame(), lines };
      expect(() => screenFrame(body)).not.toThrow();
      expect(screenFrame(body), JSON.stringify(lines)?.slice(0, 40)).toBeNull();
    }
    // The bound is inclusive and counts characters, as the server does: 20 astral ones fit 20.
    expect(screenFrame(frame({ lines: ["😀".repeat(20)] }))).not.toBeNull();
    expect(screenFrame(frame({ lines: ["x".repeat(20)] }))).not.toBeNull();
  });

  test("the largest frame the server allows is drawn in exactly its rows, and the lines are a copy", () => {
    const lines = ["a".repeat(500)];
    const f = screenFrame({ ...frame(), cols: 500, rows: 300, lines });
    expect(f).not.toBeNull();
    expect(screenLines(f!)).toHaveLength(300);
    lines.push("later"); // the peer's array is not kept: nothing can grow it after the check
    expect(f!.lines).toHaveLength(1);
  });

  test("the rows drawn are exactly `rows`, each line as it came", () => {
    const hostile = '<img src=x onerror=alert(1)> \\x1b[31m https://example.test/x';
    const f = frame({ cols: 80, rows: 4, lines: [hostile, "", "████"] });
    expect(screenLines(f)).toEqual([hostile, "", "████", ""]);
    expect(screenLines(frame({ rows: 1, lines: ["only"] }))).toEqual(["only"]);
  });
});

describe("a row keeps its columns", () => {
  test("text the monospace face covers is one run; any other character is a cell of its own", () => {
    expect(screenRuns("")).toEqual([]);
    expect(screenRuns("$ make deploy  ")).toEqual(["$ make deploy  "]);
    expect(screenRuns("TOKEN=██ x")).toEqual(["TOKEN=", { cell: "█" }, { cell: "█" }, " x"]);
    expect(screenRuns("│ok│")).toEqual([{ cell: "│" }, "ok", { cell: "│" }]);
    expect(screenRuns("é ü ÿ")).toEqual(["é ü ÿ"]); // Latin-1 is in the face
    expect(screenRuns("a😀b")).toEqual(["a", { cell: "😀" }, "b"]); // one character, one column
  });

  test("nothing is added, dropped or read as anything: the runs spell the line", () => {
    const hostile = '<b>█</b> \\x1b[31m │ https://example.test/?q=1 😀';
    const spelled = screenRuns(hostile)
      .map((r) => (typeof r === "string" ? r : r.cell))
      .join("");
    expect(spelled).toBe(hostile);
    for (const r of screenRuns(hostile)) if (typeof r !== "string") expect([...r.cell]).toHaveLength(1);
  });
});

describe("what the dialog and the rows say of a terminal", () => {
  const NOV_22 = Date.UTC(2026, 10, 22, 12) / 1000;
  const remote = (over: Partial<RemoteShare> = {}): RemoteShare => ({
    key: "peer:x/claude:y",
    peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
    peer_name: "gpu-box",
    connected: true,
    grant_id: "g7Jx9aBcDeFgHiJkLmNoPq",
    engine: "claude",
    native_id: "y",
    title: "Deploy the staging stack",
    kind: "terminal",
    level: "view",
    expires_at: NOV_22,
    revision: 1,
    seen_at: NOV_22 - 60,
    ...over,
  });

  test("the limit text says what is masked, what is not, and what is never sent, word for word", () => {
    // docs/invariants/federation.md, "The stated limits" and `vtscreen._one_cell`.
    expect(TERMINAL_LIMIT_LEAD).toBe(
      "The peer sees this terminal's screen. A secret we know of is hidden when all of it is on one line or it wraps at the screen's edge.",
    );
    expect(TERMINAL_LIMIT_UNPROTECTED).toBe(
      "A secret that is cut off, one a program lays out across lines itself, or one we don't know about is not hidden.",
    );
    expect(TERMINAL_NOT_SHOWN).toBe(
      "No screen is sent after a control we can't redraw exactly, such as an edit in the middle of a shell line, or while the screen shows any character outside plain Latin, Greek and Cyrillic text, punctuation, arrows, symbols and box drawing: emoji, CJK, Hangul, Arabic, Indic and similar scripts, or combining characters. An agent that draws emoji is often not viewable.",
    );
    expect(TERMINAL_LIMIT_DETAIL).toBe(
      "It is a picture of the screen as text. The peer cannot type, resize or scroll back.",
    );
    expect(TERMINAL_VIEW_ONLY).toBe("A terminal can only be watched.");
    // The app's copy style: no em dash.
    for (const t of [TERMINAL_LIMIT_LEAD, TERMINAL_LIMIT_UNPROTECTED, TERMINAL_NOT_SHOWN, TERMINAL_LIMIT_DETAIL])
      expect(t).not.toContain("\u2014");
  });

  test("only the server's own word makes a share a terminal", () => {
    expect(shareKind(remote())).toBe("terminal");
    for (const kind of [undefined, "api", "Terminal", "byte_stream", ""])
      expect(shareKind(remote({ kind }))).toBe("api");
  });

  test("a terminal row: the SCREEN tag and the end date, or offline", () => {
    expect(remoteRowState(remote())).toMatchObject({
      screen: true,
      offline: false,
      meta: "until 22 Nov",
      label: "Open Deploy the staging stack, shared by gpu-box, screen only",
    });
    expect(remoteRowState(remote({ connected: false }))).toMatchObject({
      screen: true,
      offline: true,
      meta: "offline",
      label: "Open Deploy the staging stack, shared by gpu-box, screen, offline",
    });
    // An API row is what it was.
    expect(remoteRowState(remote({ kind: "api" }))).toMatchObject({
      screen: false,
      meta: "view only · until 22 Nov",
      label: "Open Deploy the staging stack, shared by gpu-box, view only",
    });
    expect(remoteRowState(remote({ kind: undefined, connected: false }))).toMatchObject({
      screen: false,
      meta: "offline",
      label: "Open Deploy the staging stack, shared by gpu-box, offline",
    });
  });
});
