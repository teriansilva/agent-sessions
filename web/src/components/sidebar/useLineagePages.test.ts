/** #2325 review: a group that has loaded more than one server page refreshes (poll and Retry)
 *  through bounded requests — never one `limit` above the routes' 200 ceiling. */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { Session } from "../../types/api";
import { MAX_LIMIT, SUBAGENT_PAGE, useLineagePages } from "./useLineagePages";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { sessionChildren: vi.fn(), sessionAttention: vi.fn() } };
});

const children = vi.mocked(api.sessionChildren);
const ALL = Array.from({ length: 250 }, (_, i) => ({ id: `claude:c${i}` }) as Session);

beforeEach(() => {
  children.mockReset();
  children.mockImplementation(async (_id, q = {}) => {
    const offset = q.offset ?? 0;
    const limit = q.limit ?? 50;
    if (limit > MAX_LIMIT) throw new Error(`422: limit ${limit}`);
    const sessions = ALL.slice(offset, offset + limit);
    return {
      sessions,
      total: ALL.length,
      next_offset: offset + limit < ALL.length ? offset + limit : null,
    };
  });
});

afterEach(() => vi.useRealTimers());

async function loadAll(result: { current: ReturnType<typeof useLineagePages> }) {
  await waitFor(() => expect(result.current.items).toHaveLength(SUBAGENT_PAGE));
  while (result.current.next != null) {
    const before = result.current.items.length;
    act(() => result.current.more());
    await waitFor(() => expect(result.current.items.length).toBeGreaterThan(before));
  }
  expect(result.current.items).toHaveLength(250);
}

test("Retry after more than 200 loaded rows re-reads the window in bounded pages", async () => {
  const { result } = renderHook(() => useLineagePages("claude:m", "children", true, false, "v"));
  await loadAll(result);
  children.mockClear();
  act(() => result.current.retry());
  await waitFor(() => expect(children).toHaveBeenCalledTimes(2));
  const limits = children.mock.calls.map(([, q]) => q?.limit);
  expect(limits).toEqual([200, 50]);
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.error).toBe(false);
  expect(result.current.items).toHaveLength(250);
});

test("the 15 s poll after more than 200 loaded rows never asks past the ceiling", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { result } = renderHook(() => useLineagePages("claude:m", "children", true, false, "v"));
  await loadAll(result);
  children.mockClear();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000);
  });
  await waitFor(() => expect(children).toHaveBeenCalled());
  expect(children.mock.calls.every(([, q]) => (q?.limit ?? 0) <= MAX_LIMIT)).toBe(true);
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.error).toBe(false);
  expect(result.current.items).toHaveLength(250);
});
