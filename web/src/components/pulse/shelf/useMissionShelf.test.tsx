/** The shelf's reads never stack (review on #1904): a read slower than the poll period must land,
 *  not be invalidated by the next tick's read, over and over. */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../../lib/api";
import { SHELF_POLL_MS, SHELF_READ_TIMEOUT_MS, useMissionShelf } from "./useMissionShelf";

vi.mock("../../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../../lib/api")>("../../../lib/api");
  return { ...actual, api: { missionShelf: vi.fn() } };
});

const EMPTY = {
  mission_id: "msn_1",
  revision: 3,
  counts: {
    total: 0,
    by_kind: { image: 0, video: 0, audio: 0, document: 0, code: 0, link: 0, text: 0 },
    by_source: { delivered: 0, found: 0 },
    unreviewed: 0,
    file_checks: { changed: 0, missing: 0, inaccessible: 0 },
  },
  items: [],
  next_cursor: null,
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("a slow read is not overtaken by the timer: no second read starts, and the first lands", async () => {
  let settle!: (v: unknown) => void;
  vi.mocked(api.missionShelf).mockImplementationOnce(
    () => new Promise((res) => (settle = res)) as ReturnType<typeof api.missionShelf>,
  );
  const { result } = renderHook(() => useMissionShelf("msn_1"));
  expect(api.missionShelf).toHaveBeenCalledTimes(1);
  await act(async () => {
    vi.advanceTimersByTime(SHELF_POLL_MS * 3);
  });
  expect(api.missionShelf).toHaveBeenCalledTimes(1);
  await act(async () => {
    settle(EMPTY);
  });
  expect(result.current.shelf?.revision).toBe(3);
  vi.mocked(api.missionShelf).mockResolvedValue(EMPTY as never);
  await act(async () => {
    vi.advanceTimersByTime(SHELF_POLL_MS);
  });
  expect(api.missionShelf).toHaveBeenCalledTimes(2);
});

test("a read that never settles is aborted, and polling goes on", async () => {
  vi.mocked(api.missionShelf).mockReset();
  vi.mocked(api.missionShelf).mockImplementationOnce(
    (_m, _c, signal) =>
      new Promise((_res, rej) =>
        signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError"))),
      ) as ReturnType<typeof api.missionShelf>,
  );
  vi.mocked(api.missionShelf).mockResolvedValue(EMPTY as never);
  const { result } = renderHook(() => useMissionShelf("msn_1"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(SHELF_READ_TIMEOUT_MS - 1);
  });
  expect(api.missionShelf).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(SHELF_POLL_MS + 1);
  });
  expect(vi.mocked(api.missionShelf).mock.calls.length).toBeGreaterThan(1);
  expect(result.current.shelf?.revision).toBe(3);
});
