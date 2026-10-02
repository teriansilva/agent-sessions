import { afterEach, beforeEach, expect, test, vi } from "vitest";
import {
  createBootReadyGate,
  READY_MAX_MS,
  READY_QUIET_AFTER_PASTE_ENABLE_MS,
  READY_QUIET_MS,
} from "./bootReady";

const bytes = (s: string) => new TextEncoder().encode(s);

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("codex-style boot (no ?2004h): every chunk restarts the quiet window (#607)", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  gate.note(bytes("loading codex…"));
  vi.advanceTimersByTime(READY_QUIET_MS - 1);
  expect(onReady).not.toHaveBeenCalled();

  gate.note(bytes("still preparing…")); // restarts the window — not READY_QUIET_MS from byte one
  vi.advanceTimersByTime(READY_QUIET_MS - 1);
  expect(onReady).not.toHaveBeenCalled();

  vi.advanceTimersByTime(1);
  expect(onReady).toHaveBeenCalledTimes(1);
});

test("claude-style boot: ?2004h does NOT ready instantly — the clear that follows would wipe the paste (#616)", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  // Pre-TUI setup: bracketed paste armed at byte ~25, long before the first paint settles.
  gate.note(bytes("\x1b[?25h\x1b[?25l\x1b[?2004h"));
  expect(onReady).not.toHaveBeenCalled(); // the bug: this used to fire markInputReady() here

  // Alt-screen switch + clear: on the old code the paste had already landed and is now gone.
  gate.note(bytes("\x1b[?1049h\x1b[2J\x1b[H"));
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS - 1);
  expect(onReady).not.toHaveBeenCalled();

  gate.note(bytes("\x1b[?1000h✳ Claude Code v2.1.206")); // banner still painting → restart
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS - 1);
  expect(onReady).not.toHaveBeenCalled();

  vi.advanceTimersByTime(1); // paint settled
  expect(onReady).toHaveBeenCalledTimes(1);
});

test("?2004h shortens the quiet window", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  gate.note(bytes("booting"));
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS);
  expect(onReady).not.toHaveBeenCalled(); // no paste-enable yet → the long window applies

  gate.note(bytes("\x1b[?2004h"));
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS);
  expect(onReady).toHaveBeenCalledTimes(1);
});

test("?2004h split across chunk boundaries is still seen", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  gate.note(bytes("\x1b[?200"));
  gate.note(bytes("4h\x1b[2J")); // completes the sequence across the carry
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS);
  expect(onReady).toHaveBeenCalledTimes(1);
});

test("a never-quiet agent still becomes ready at the ceiling", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  // A spinner painting forever: the quiet window never expires, but the ceiling runs from byte one.
  for (let t = 0; t < READY_MAX_MS; t += 100) {
    gate.note(bytes("."));
    vi.advanceTimersByTime(100);
  }
  expect(onReady).toHaveBeenCalledTimes(1);
});

test("onReady fires at most once, and never after dispose", () => {
  const onReady = vi.fn();
  const gate = createBootReadyGate(onReady);

  gate.note(bytes("\x1b[?2004h"));
  vi.advanceTimersByTime(READY_QUIET_AFTER_PASTE_ENABLE_MS * 3);
  gate.note(bytes("more output"));
  vi.advanceTimersByTime(READY_MAX_MS);
  expect(onReady).toHaveBeenCalledTimes(1);

  const onReady2 = vi.fn();
  const gate2 = createBootReadyGate(onReady2);
  gate2.note(bytes("booting"));
  gate2.dispose();
  vi.advanceTimersByTime(READY_MAX_MS * 2);
  expect(onReady2).not.toHaveBeenCalled();
});
