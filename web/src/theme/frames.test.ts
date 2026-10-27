/** The one shared frame scheduler (#1727): one rAF loop, only while a job runs. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";

let scheduleFrames: typeof import("./frames").scheduleFrames;
let scheduledFrames: typeof import("./frames").scheduledFrames;
let pending: Map<number, FrameRequestCallback>;
let next: number;

beforeEach(async () => {
  // A fresh module per test: its loop state never carries from one test into the next.
  vi.resetModules();
  ({ scheduleFrames, scheduledFrames } = await import("./frames"));
  pending = new Map();
  next = 1;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    pending.set(next, cb);
    return next++;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => pending.delete(id));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

/** Run every frame callback requested so far, once (one browser frame). */
function frame(now: number) {
  const due = [...pending.values()];
  pending.clear();
  for (const cb of due) cb(now);
}

test("a job scheduled by another job mid-frame joins the same loop — never a second one", () => {
  let inner = 0;
  const innerJob = () => ++inner < 3;
  let outerRuns = 0;
  scheduleFrames(() => {
    if (++outerRuns === 1) scheduleFrames(innerJob);
    return outerRuns < 3;
  });
  expect(pending.size).toBe(1);
  frame(16);
  expect(pending.size).toBe(1); // ONE frame requested for both jobs (review of #1747: it was two)
  frame(32); // both jobs run in this one frame
  expect(pending.size).toBe(1);
  expect(scheduledFrames()).toBe(2);
  frame(48); // the outer job is done
  expect(pending.size).toBe(1);
  expect(scheduledFrames()).toBe(1);
  frame(64);
  expect(scheduledFrames()).toBe(0);
  expect(pending.size).toBe(0); // nothing left: no frame is requested while idle
  expect(inner).toBe(3);
});

test("an idle scheduler requests no frame, and cancelling the last job cancels its frame", () => {
  expect(pending.size).toBe(0);
  const cancel = scheduleFrames(() => true);
  expect(pending.size).toBe(1);
  cancel();
  expect(pending.size).toBe(0);
  expect(scheduledFrames()).toBe(0);
});

test("one job that throws does not stop the others", () => {
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  let ran = 0;
  scheduleFrames(() => {
    throw new Error("broken effect");
  });
  scheduleFrames(() => ++ran < 2);
  frame(16);
  frame(32);
  expect(ran).toBe(2);
  expect(scheduledFrames()).toBe(0);
  expect(error).toHaveBeenCalled();
  error.mockRestore();
});
