import { afterEach, expect, test } from "vitest";
import type { StructuredSnapshot, StructuredTurn } from "../../types/api";
import { ALWAYS_SEND_NOW_KEY, autoSendNowHead, readAlwaysSendNow } from "./alwaysSendNow";

const turn = (turn_id: string, state: string, over: Partial<StructuredTurn> = {}) =>
  ({ turn_id, state, text: turn_id, tools: [], ...over }) as unknown as StructuredTurn;

function snap(turns: StructuredTurn[], over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    active_turn: "a",
    turns,
    native: { native_id: "n", worker: "w", background_active: false, send_now: "interrupt" },
    ...over,
  } as unknown as StructuredSnapshot;
}

afterEach(() => localStorage.clear());

test("the first queued message is sent next, only while Send now is offered", () => {
  const turns = [turn("a", "running"), turn("q1", "queued"), turn("q2", "queued")];
  expect(autoSendNowHead(snap(turns))).toBe("q1");
  // No target (older worker, or nothing running): nothing to send past.
  expect(autoSendNowHead(snap(turns, { native: { native_id: "n", worker: "w", background_active: false } }))).toBeNull();
  expect(autoSendNowHead(snap(turns, { active_turn: null }))).toBeNull();
  expect(autoSendNowHead(null)).toBeNull();
});

test("never while another selection is in flight, and never a refused one again", () => {
  const a = turn("a", "running");
  expect(autoSendNowHead(snap([a, turn("q1", "queued", { delivery: "interrupting" }), turn("q2", "queued")]))).toBeNull();
  expect(autoSendNowHead(snap([a, turn("q1", "delivering"), turn("q2", "queued")]))).toBeNull();
  expect(autoSendNowHead(snap([a, turn("q1", "queued", { delivery: "interrupt_failed" }), turn("q2", "queued")]))).toBeNull();
  // A later message's selection (an older tab) holds the head too.
  expect(autoSendNowHead(snap([a, turn("q1", "queued"), turn("q2", "queued", { delivery: "interrupting" })]))).toBeNull();
});

test("the preference is off unless this device turned it on", () => {
  expect(readAlwaysSendNow()).toBe(false);
  localStorage.setItem(ALWAYS_SEND_NOW_KEY, "1");
  expect(readAlwaysSendNow()).toBe(true);
});
