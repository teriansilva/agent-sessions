/** A terminal a peer shares, read as its screen (#2060): the frame loop's state machine, pure.
 *
 *  The owner answers `GET …/remote/{peer}/{grant}/frame` with exactly one of three shapes
 *  (`federation/remote.py` `check_frame`), or refuses. This module says what the pane shows for
 *  each, and nothing here touches the network, a clock or storage: the clock is an argument
 *  (`at`, in milliseconds), so every rule below is a table row in `federationScreen.test.ts`.
 *
 *  Three rules it must keep:
 *
 *  - **`have` is the fingerprint of the frame on screen**, and of nothing else. It is DERIVED
 *    from the frame (`screenHave`), never kept beside it, so dropping the frame drops it: an
 *    empty pane asks for the current frame and can never be told `unchanged`.
 *  - **A dimmed frame is bounded.** While the owner keeps answering `settling` the last frame
 *    stays, dimmed and marked; after `SCREEN_STALE_MS` it is dropped, so an old screen is never
 *    left looking current.
 *  - **Every transition that drops the frame bumps the generation** (the fence of #2055's pane):
 *    a request that started before it is aborted, and its answer is discarded here even if it
 *    arrives.
 */
import { REMOTE_RETRY_MS, REMOTE_STALL_LIMIT } from "./federation";

/** How long a frame read is held by the owner, in seconds. Through Home Free a proxied read is
 *  cut at 5 s, so the hold stays under it everywhere (the same value as the events hold). */
export const REMOTE_FRAME_WAIT_S = 3;
/** How long a dimmed frame may stay while the owner keeps answering `settling`. */
export const SCREEN_STALE_MS = 30_000;
/** The least time between two frame reads: an owner that answers at once is not asked in a
 *  tight loop. */
export const SCREEN_MIN_INTERVAL_MS = 1_000;

/** The server's bounds (`frames.ROWS_MAX`, `frames.COLS_MAX`); a frame outside them is not one. */
const ROWS_MAX = 300;
const COLS_MAX = 500;
const FINGERPRINT_RE = /^[0-9a-f]{32}$/;

export interface RemoteScreenFrame {
  fingerprint: string;
  cols: number;
  rows: number;
  lines: string[];
  settled: boolean;
}

/** What a frame read answers with a 200. */
export type RemoteFrameAnswer =
  | RemoteScreenFrame
  | { fingerprint: string; unchanged: true }
  | { settling: true };

/** `loading`: nothing answered yet. `live`: the owner is answering (a frame, or `settling` with
 *  none yet). `unavailable`: the owner cannot render its screen right now. `offline`: the owner
 *  is not connected. `gone`: the share ended; final. */
export type ScreenStatus = "loading" | "live" | "unavailable" | "offline" | "gone";

export interface ScreenState {
  /** `<peerId>/<grantId>` of the route this state is for. */
  target: string;
  /** THE FENCE. Every read carries the generation it started under; an answer under any other
   *  is discarded. Bumped whenever the frame is dropped. */
  generation: number;
  status: ScreenStatus;
  /** Milliseconds to wait before this generation's first read; `null` = read nothing (gone). */
  startAfterMs: number | null;
  /** The frame on screen: only ever one this pane received in this view. */
  frame: RemoteScreenFrame | null;
  /** The *updating…* marker. */
  updating: boolean;
  /** The frame is the last one received while a new one is awaited: drawn dimmed. */
  dimmed: boolean;
  /** When the frame on screen began waiting for its successor (the 30 s bound's clock). */
  staleSince: number | null;
  /** Reads in a row that failed without saying anything about the grant or the screen. */
  stalls: number;
  /** Why the screen is `unavailable`: the owner said so (`screen`), it sent something that is not
   *  a frame this pane can draw (`frame`), or reads kept failing without a word (`read`). Only
   *  the first is the renderer's refusal, so only it is explained with what is never sent. */
  reason: UnavailableReason | null;
}

export type UnavailableReason = "screen" | "frame" | "read";

export type ScreenEvent =
  | { type: "open"; target: string }
  | { type: "answer"; generation: number; answer: unknown; at: number }
  | { type: "failed"; generation: number; status: number; code: string | null; at: number }
  | { type: "tick"; at: number };

export function screenInit(target: string): ScreenState {
  return {
    target,
    generation: 0,
    status: "loading",
    startAfterMs: 0,
    frame: null,
    updating: false,
    dimmed: false,
    staleSince: null,
    stalls: 0,
    reason: null,
  };
}

/** The fingerprint the next read sends: the frame on screen's, or none. */
export function screenHave(state: ScreenState): string | null {
  return state.frame ? state.frame.fingerprint : null;
}

/** Drop the frame (and with it `have`) and start a new generation. */
function cleared(
  state: ScreenState,
  status: ScreenStatus,
  startAfterMs: number | null,
  over: Partial<ScreenState> = {},
): ScreenState {
  return {
    target: state.target,
    generation: state.generation + 1,
    status,
    startAfterMs,
    frame: null,
    updating: false,
    dimmed: false,
    staleSince: null,
    stalls: 0,
    reason: null,
    ...over,
  };
}

/** Whether `v` is a whole number in `1..max`. A peer's number is untrusted (#2055 review): `1e300`
 *  is an integer to `Number.isInteger`, so the upper bound is what keeps it out. */
const inRange = (v: unknown, max: number): v is number =>
  typeof v === "number" && Number.isInteger(v) && v >= 1 && v <= max;

/** A frame fit to draw, or `null`. The server validated it already (`check_frame`); this is the
 *  browser not trusting a shape it is about to index into. Every number the pane sizes, pads or
 *  loops by (`rows`, `cols`, each line's length) is checked here against the server's own bounds
 *  BEFORE anything is allocated from it: `rows` and `cols` must be whole numbers in range, there
 *  are no more lines than `rows`, and each line is a string of at most `cols` characters, measured
 *  without building anything as long as the line. Lines stay strings: they are only ever handed
 *  to React as text. */
export function screenFrame(raw: unknown): RemoteScreenFrame | null {
  if (!raw || typeof raw !== "object") return null;
  const x = raw as Record<string, unknown>;
  const { fingerprint, cols, rows, lines, settled } = x;
  if (typeof fingerprint !== "string" || !FINGERPRINT_RE.test(fingerprint)) return null;
  if (!inRange(cols, COLS_MAX) || !inRange(rows, ROWS_MAX)) return null;
  if (typeof settled !== "boolean" || !Array.isArray(lines)) return null;
  if (lines.length > rows) return null;
  for (const l of lines) if (typeof l !== "string" || !fitsColumns(l, cols)) return null;
  return { fingerprint, cols, rows, lines: [...(lines as string[])], settled };
}

/** Whether `line` has at most `cols` characters (code points, as the server counts them). It
 *  stops at the first one past `cols`, so a line of any length costs at most `cols` steps. */
function fitsColumns(line: string, cols: number): boolean {
  if (line.length <= cols) return true; // UTF-16 units: never fewer than the characters
  let n = 0;
  for (let i = 0; i < line.length; i++) {
    const unit = line.charCodeAt(i);
    // A high surrogate followed by a low one is ONE character (an astral code point).
    if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < line.length) {
      const next = line.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i++;
    }
    if (++n > cols) return false;
  }
  return true;
}

/** What a failed frame read means, from its status and the server's `code`. */
export function screenFailure(
  status: number,
  code: string | null,
): "gone" | "offline" | "unavailable" | "stall" {
  if (status === 404) return "gone";
  if (status === 503 && code === "peer_offline") return "offline";
  if (status === 503 && code === "unavailable") return "unavailable";
  return "stall";
}

/** The last frame stays, dimmed and marked, and its clock runs from the first time it waited. */
function waiting(state: ScreenState, at: number): ScreenState {
  if (!state.frame)
    return { ...state, status: "live", reason: null, updating: true, dimmed: false, staleSince: null };
  return {
    ...state,
    status: "live",
    reason: null,
    updating: true,
    dimmed: true,
    staleSince: state.staleSince ?? at,
  };
}

export function screenReduce(state: ScreenState, event: ScreenEvent): ScreenState {
  if (event.type === "open")
    return event.target === state.target ? state : cleared({ ...state, target: event.target }, "loading", 0);
  if (event.type === "tick") {
    // The 30 s bound. Dropping the frame drops `have` with it (it is derived), and the new
    // generation discards a read that was still asking with the old fingerprint.
    if (state.staleSince === null || event.at - state.staleSince < SCREEN_STALE_MS) return state;
    return cleared(state, "live", 0, { updating: true });
  }
  if (event.generation !== state.generation) return state; // an answer from before a clear
  if (event.type === "failed") {
    const kind = screenFailure(event.status, event.code);
    if (kind === "gone") return cleared(state, "gone", null);
    if (kind === "offline") return cleared(state, "offline", REMOTE_RETRY_MS);
    if (kind === "unavailable")
      return cleared(state, "unavailable", REMOTE_RETRY_MS, { reason: "screen" });
    // Nothing was said about the screen: what is shown is no longer known to be current.
    if (state.stalls + 1 >= REMOTE_STALL_LIMIT)
      return cleared(state, "unavailable", REMOTE_RETRY_MS, {
        stalls: REMOTE_STALL_LIMIT,
        reason: "read",
      });
    return {
      ...waiting(state, event.at),
      status: state.status,
      reason: state.reason,
      stalls: state.stalls + 1,
    };
  }
  const answer = event.answer as Record<string, unknown> | null;
  if (answer && typeof answer === "object" && answer.settling === true)
    return { ...waiting(state, event.at), stalls: 0 };
  if (answer && typeof answer === "object" && answer.unchanged === true) {
    // The owner just confirmed THIS frame is the current, settled screen. For any other
    // fingerprint, or with nothing on screen, the answer is about a frame this pane does not
    // show: it changes nothing.
    if (!state.frame || answer.fingerprint !== state.frame.fingerprint) return state;
    return {
      ...state,
      status: "live",
      reason: null,
      updating: false,
      dimmed: false,
      staleSince: null,
      stalls: 0,
    };
  }
  const frame = screenFrame(answer);
  // Not one of the three shapes, or a frame that fails the check: it is "not viewable", never
  // thrown on and never half drawn. What was on screen is dropped with its `have` (the owner's
  // current screen is not that one any more), and the next read asks after the usual pause.
  if (!frame) return cleared(state, "unavailable", REMOTE_RETRY_MS, { reason: "frame" });
  // A frame replaces whatever was shown, at full strength. One taken while the screen was still
  // changing (the owner's busy bound) is fresh but marked.
  return {
    ...state,
    status: "live",
    frame,
    updating: !frame.settled,
    dimmed: false,
    staleSince: null,
    stalls: 0,
    reason: null,
  };
}

/** When the pane must be told the time again (`tick`), or `null` when no clock is running. */
export function screenDeadline(state: ScreenState): number | null {
  return state.staleSince === null ? null : state.staleSince + SCREEN_STALE_MS;
}

/** The banner's connection word and LED: green only while the owner is answering. */
export function screenBadge(state: ScreenState): { led: "up" | "idle"; word: string } {
  const answering = state.status === "live" || state.status === "unavailable";
  if (answering && state.stalls === 0) return { led: "up", word: "connected" };
  if (answering) return { led: "idle", word: "reconnecting" };
  if (state.status === "gone") return { led: "idle", word: "unavailable" };
  if (state.status === "offline") return { led: "idle", word: "offline" };
  return { led: "idle", word: "connecting" };
}

/** The frame's rows as the pane draws them: exactly `rows` lines, each the text it carries (a
 *  short line is short; the block's width is the frame's `cols`, not the longest line). */
export function screenLines(frame: RemoteScreenFrame): string[] {
  const out = frame.lines.slice(0, frame.rows);
  while (out.length < frame.rows) out.push("");
  return out;
}

/** One row as the pane draws it: runs of text the bundled monospace face has a glyph for, and
 *  every other character on its own (a `cell`).
 *
 *  A frame is a grid: one character per column. The bundled face is a Latin subset, so a masked
 *  run (`█`), a box-drawing line or any other character falls back to another font, whose advance
 *  is not this one's; left inline, everything after it on the row would drift off its column. The
 *  pane gives each such character a box exactly one column wide. Both kinds are handed to React
 *  as strings and end up as text nodes: nothing here inspects what a character means. */
export type ScreenRun = string | { cell: string };
const OUTSIDE_FACE = /[^\u0020-\u007e\u00a0-\u00ff]/u;
export function screenRuns(line: string): ScreenRun[] {
  if (!OUTSIDE_FACE.test(line)) return line ? [line] : [];
  const out: ScreenRun[] = [];
  let text = "";
  for (const ch of line) {
    if (!OUTSIDE_FACE.test(ch)) {
      text += ch;
      continue;
    }
    if (text) out.push(text);
    text = "";
    out.push({ cell: ch });
  }
  if (text) out.push(text);
  return out;
}

export const SCREEN_UPDATING = "updating…";
export const SCREEN_UNAVAILABLE = "This screen can't be shown right now.";
export const SCREEN_UNAVAILABLE_DETAIL = "It will appear when the screen can be read again.";
/** Said instead of the limits when what arrived is not a frame this pane can draw. */
export const SCREEN_NOT_A_FRAME = "What arrived is not a screen this page can draw.";
export const SCREEN_ONLY = "Screen only.";
export const SCREEN_ONLY_DETAIL = "No typing, no scrollback.";
