import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../lib/api";
import {
  MIN_REFRESH_MS,
  noteKeys,
  refreshOrigins,
  resetOriginsForTest,
} from "./automationOrigins";

beforeEach(() => {
  resetOriginsForTest();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("a steady list asks nothing; a row not seen before asks once (#1201)", async () => {
  const spy = vi.spyOn(api, "automationOrigins").mockResolvedValue({ origins: {} });
  await refreshOrigins(true);
  expect(spy).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(MIN_REFRESH_MS + 1);
  noteKeys(["claude:a", "claude:b"]);
  await vi.runAllTimersAsync();
  expect(spy).toHaveBeenCalledTimes(2);
  noteKeys(["claude:a", "claude:b"]);
  vi.advanceTimersByTime(MIN_REFRESH_MS + 1);
  await vi.runAllTimersAsync();
  expect(spy).toHaveBeenCalledTimes(2);
});

test("a request inside the window is deferred to its end, never dropped", async () => {
  const spy = vi.spyOn(api, "automationOrigins").mockResolvedValue({ origins: {} });
  await refreshOrigins(true);
  noteKeys(["claude:new"]);
  noteKeys(["claude:newer"]);
  expect(spy).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(MIN_REFRESH_MS);
  expect(spy).toHaveBeenCalledTimes(2);
});

test("a row that appears while a read is in flight gets ONE trailing read, and its badge", async () => {
  vi.useRealTimers();
  const answers: ((v: { origins: Record<string, never> | Record<string, unknown> }) => void)[] = [];
  const spy = vi.spyOn(api, "automationOrigins").mockImplementation(
    () => new Promise((res) => answers.push(res as never)),
  );
  const { getOriginsForTest } = await import("./automationOrigins");
  const first = refreshOrigins(true);
  await vi.waitFor(() => expect(answers).toHaveLength(1)); // the first read is on the wire
  // The session appears while the first read is held: it is NOT marked seen by that read.
  noteKeys(["claude:new"]);
  noteKeys(["claude:newer"]);
  answers[0]({ origins: {} });
  await first;
  await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2)); // exactly one trailing read
  answers[1]({
    origins: {
      "claude:new": { kind: "session", automation_id: "a1", name: "Nightly", run_id: "r", deleted: false },
    },
  });
  await vi.waitFor(() => expect(getOriginsForTest()["claude:new"]?.name).toBe("Nightly"));
  expect(spy).toHaveBeenCalledTimes(2);
});

test("after a FAILED read, the same new key asks again on the next report", async () => {
  let fail = true;
  const spy = vi.spyOn(api, "automationOrigins").mockImplementation(async () => {
    if (fail) throw new Error("down");
    return { origins: {} };
  });
  noteKeys(["claude:x"]); // the first report reads the map at once, and that read fails
  await vi.advanceTimersByTimeAsync(0);
  expect(spy).toHaveBeenCalledTimes(1);
  fail = false;
  noteKeys(["claude:x"]); // the list's next poll reports the same row: it asks again
  await vi.advanceTimersByTimeAsync(MIN_REFRESH_MS + 1);
  expect(spy).toHaveBeenCalledTimes(2);
  // Answered now: a steady list asks nothing more.
  noteKeys(["claude:x"]);
  await vi.advanceTimersByTimeAsync(MIN_REFRESH_MS + 1);
  expect(spy).toHaveBeenCalledTimes(2);
});

test("a failed read retries on a capped backoff for unchanged rows, then stops once answered", async () => {
  const { getOriginsForTest, RETRY_MS } = await import("./automationOrigins");
  let failures = 3;
  const spy = vi.spyOn(api, "automationOrigins").mockImplementation(async () => {
    if (failures-- > 0) throw new Error("down");
    return {
      origins: {
        "claude:x": { kind: "session", automation_id: "a1", name: "Nightly", run_id: "r", deleted: false },
      },
    };
  });
  noteKeys(["claude:x"]); // reported once; the list never changes after this
  await vi.advanceTimersByTimeAsync(0);
  expect(spy).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(RETRY_MS[0]);
  expect(spy).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(RETRY_MS[1]);
  expect(spy).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(RETRY_MS[2]);
  expect(spy).toHaveBeenCalledTimes(4);
  expect(getOriginsForTest()["claude:x"]?.name).toBe("Nightly");
  // Answered: no storm, no more reads.
  await vi.advanceTimersByTimeAsync(10 * 60_000);
  expect(spy).toHaveBeenCalledTimes(4);
});

test("a down server is asked at most once per capped interval", async () => {
  const { RETRY_MS } = await import("./automationOrigins");
  const spy = vi.spyOn(api, "automationOrigins").mockRejectedValue(new Error("down"));
  noteKeys(["claude:x"]);
  await vi.advanceTimersByTimeAsync(30 * 60_000);
  const expected = 1 + 2 + Math.floor((30 * 60_000 - RETRY_MS[0] - RETRY_MS[1]) / RETRY_MS[2]);
  expect(spy.mock.calls.length).toBeLessThanOrEqual(expected);
  expect(spy.mock.calls.length).toBeGreaterThanOrEqual(expected - 1);
});

/** Stand-in for the tab's visibility: jsdom's `document.hidden` is a prototype getter, so an own
 *  property shadows it and deleting that property restores it. */
function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

test("a retry that comes due while the tab is hidden waits for the tab to be visible", async () => {
  const { RETRY_MS } = await import("./automationOrigins");
  let fail = true;
  const spy = vi.spyOn(api, "automationOrigins").mockImplementation(async () => {
    if (fail) throw new Error("down");
    return { origins: {} };
  });
  try {
    noteKeys(["claude:x"]);
    await vi.advanceTimersByTimeAsync(0);
    expect(spy).toHaveBeenCalledTimes(1); // failed: a retry is scheduled
    fail = false;
    setHidden(true);
    await vi.advanceTimersByTimeAsync(RETRY_MS[0]);
    expect(spy).toHaveBeenCalledTimes(1); // due, but hidden: no background traffic
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(spy).toHaveBeenCalledTimes(1);
    setHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(spy).toHaveBeenCalledTimes(2); // visible again: exactly one retry
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(spy).toHaveBeenCalledTimes(2); // answered: nothing more
  } finally {
    delete (document as unknown as { hidden?: boolean }).hidden;
  }
});

test("a success resets the backoff: the next failure retries after the FIRST delay again", async () => {
  const { RETRY_MS } = await import("./automationOrigins");
  let fail = true;
  const spy = vi.spyOn(api, "automationOrigins").mockImplementation(async () => {
    if (fail) throw new Error("down");
    return { origins: {} };
  });
  noteKeys(["claude:x"]);
  await vi.advanceTimersByTimeAsync(0);
  expect(spy).toHaveBeenCalledTimes(1); // fail
  fail = false;
  await vi.advanceTimersByTimeAsync(RETRY_MS[0]);
  expect(spy).toHaveBeenCalledTimes(2); // succeed
  fail = true;
  await vi.advanceTimersByTimeAsync(MIN_REFRESH_MS + 1);
  noteKeys(["claude:y"]); // a new row asks, and that read fails
  await vi.advanceTimersByTimeAsync(0);
  expect(spy).toHaveBeenCalledTimes(3);
  fail = false;
  await vi.advanceTimersByTimeAsync(RETRY_MS[0] - 1);
  expect(spy).toHaveBeenCalledTimes(3);
  await vi.advanceTimersByTimeAsync(1);
  expect(spy).toHaveBeenCalledTimes(4); // 5 s, not the 30 s the step before the success had reached
});
