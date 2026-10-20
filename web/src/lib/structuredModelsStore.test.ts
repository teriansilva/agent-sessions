import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useStructuredModels } from "../app/useStructuredModels";
import type { StructuredModelList } from "../types/api";
import { ApiError, api } from "./api";
import {
  listAgeMinutes,
  STALE_RECHECK_MS,
  STALE_RECHECK_WINDOW_MS,
  structuredModelsStore,
} from "./structuredModelsStore";

const list = (...ids: string[]): StructuredModelList => ({
  status: "ok",
  reason: null,
  models: ids.map((id) => ({ id, label: id, description: null, efforts: [], is_default: false })),
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const ids = (engine: string) => structuredModelsStore.get(engine).list?.models.map((m) => m.id);

beforeEach(() => structuredModelsStore.reset());
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

test("a revalidation stores the answer per engine (#1503)", async () => {
  vi.spyOn(api, "structuredModels").mockImplementation(async (e) => list(`${e}-model`));
  expect(structuredModelsStore.get("codex-api").list).toBeNull();
  await structuredModelsStore.revalidate("codex-api");
  await structuredModelsStore.revalidate("claude-api");
  expect(ids("codex-api")).toEqual(["codex-api-model"]);
  expect(ids("claude-api")).toEqual(["claude-api-model"]);
  expect(structuredModelsStore.get("codex-api").revalidating).toBe(false);
});

test("the hook paints the cached list at once and revalidates behind it (#1503)", async () => {
  const spy = vi.spyOn(api, "structuredModels").mockResolvedValueOnce(list("old"));
  await structuredModelsStore.revalidate("codex-api");
  const next = deferred<StructuredModelList>();
  spy.mockReturnValueOnce(next.promise);
  const { result } = renderHook(() => useStructuredModels("codex-api"));
  // First render: the cached list, with the revalidation already running behind it.
  expect(result.current.list?.models.map((m) => m.id)).toEqual(["old"]);
  expect(result.current.revalidating).toBe(true);
  expect(spy).toHaveBeenCalledTimes(2);
  await act(async () => next.resolve(list("new")));
  expect(result.current.list?.models.map((m) => m.id)).toEqual(["new"]);
  expect(result.current.revalidating).toBe(false);
});

test("a console agent (null engine) asks nothing (#1503)", () => {
  const spy = vi.spyOn(api, "structuredModels");
  const { result } = renderHook(() => useStructuredModels(null));
  expect(result.current.list).toBeNull();
  expect(spy).not.toHaveBeenCalled();
});

test("requests are single-flight per engine; a forced one supersedes the one in flight (#1503)", async () => {
  const first = deferred<StructuredModelList>();
  const second = deferred<StructuredModelList>();
  const spy = vi
    .spyOn(api, "structuredModels")
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  const a = structuredModelsStore.revalidate("codex-api");
  const b = structuredModelsStore.revalidate("codex-api");
  expect(spy).toHaveBeenCalledTimes(1); // joined, not a second request
  const forced = structuredModelsStore.revalidate("codex-api", { force: true });
  expect(spy).toHaveBeenCalledTimes(2);
  second.resolve(list("newer"));
  await forced;
  first.resolve(list("older")); // lands last — and is dropped, generation-guarded
  await Promise.all([a, b]);
  expect(ids("codex-api")).toEqual(["newer"]);
});

test("a commit for a superseded generation is refused (#1503)", () => {
  const gen = structuredModelsStore.generation("codex-api");
  expect(structuredModelsStore.commit("codex-api", gen + 1, list("x"))).toBe(false);
  expect(structuredModelsStore.get("codex-api").list).toBeNull();
});

test("a failed request keeps the last list and notes why (#1503)", async () => {
  const spy = vi.spyOn(api, "structuredModels").mockResolvedValueOnce(list("kept"));
  await structuredModelsStore.revalidate("codex-api");
  spy.mockRejectedValueOnce(new ApiError(502, "bad gateway"));
  await structuredModelsStore.revalidate("codex-api");
  const snap = structuredModelsStore.get("codex-api");
  expect(ids("codex-api")).toEqual(["kept"]);
  expect(snap.error).toBe("bad gateway");
  // …and a success clears the note.
  spy.mockResolvedValueOnce(list("kept", "new"));
  await structuredModelsStore.revalidate("codex-api");
  expect(structuredModelsStore.get("codex-api").error).toBeNull();
});

test("a failed FIRST request is the answer: unavailable, with the reason (#1503)", async () => {
  vi.spyOn(api, "structuredModels").mockRejectedValueOnce(new TypeError("offline"));
  await structuredModelsStore.revalidate("codex-api");
  expect(structuredModelsStore.get("codex-api").list).toEqual({
    status: "unavailable",
    models: [],
    reason: "it could not be asked",
  });
});

test("a stale answer is asked again until the window ends, then the polling stops (#1503)", async () => {
  vi.useFakeTimers();
  const spy = vi
    .spyOn(api, "structuredModels")
    .mockResolvedValue({ ...list("a"), stale: true, fetched_age_s: 700 });
  await structuredModelsStore.revalidate("codex-api");
  await vi.advanceTimersByTimeAsync(STALE_RECHECK_WINDOW_MS + STALE_RECHECK_MS * 5);
  const calls = spy.mock.calls.length;
  expect(calls).toBeGreaterThanOrEqual(STALE_RECHECK_WINDOW_MS / STALE_RECHECK_MS);
  expect(calls).toBeLessThanOrEqual(STALE_RECHECK_WINDOW_MS / STALE_RECHECK_MS + 2);
  await vi.advanceTimersByTimeAsync(STALE_RECHECK_MS * 10);
  expect(spy.mock.calls.length).toBe(calls); // bounded: no polling after the window
});

test("a slow server refresh (its full 20 s deadline and more) still reaches a mounted page (#1503 PR review)", async () => {
  vi.useFakeTimers();
  const t0 = Date.now();
  const spy = vi.spyOn(api, "structuredModels").mockImplementation(async () =>
    // The server keeps answering "stale" while its probe runs: 20 s deadline + 5 s kill + a
    // second client's probe queued ahead of it.
    Date.now() - t0 < 45_000 ? { ...list("old"), stale: true } : list("old", "new"),
  );
  const { result } = renderHook(() => useStructuredModels("codex-api"));
  await act(() => vi.advanceTimersByTimeAsync(50_000));
  expect(result.current.list?.models.map((m) => m.id)).toEqual(["old", "new"]);
  expect(result.current.list?.stale).toBeFalsy();
  const calls = spy.mock.calls.length;
  await act(() => vi.advanceTimersByTimeAsync(STALE_RECHECK_MS * 5));
  expect(spy.mock.calls.length).toBe(calls); // a fresh answer ends the polling
});

test("a fresh answer after a stale one ends the rechecks; a failed refresh never starts them (#1503)", async () => {
  vi.useFakeTimers();
  const spy = vi
    .spyOn(api, "structuredModels")
    .mockResolvedValueOnce({ ...list("a"), stale: true })
    .mockResolvedValueOnce(list("a", "b"))
    .mockResolvedValue({ ...list("a"), stale: true, refresh_error: "offline" });
  await structuredModelsStore.revalidate("codex-api");
  await vi.advanceTimersByTimeAsync(STALE_RECHECK_MS * 3);
  expect(spy).toHaveBeenCalledTimes(2);
  expect(ids("codex-api")).toEqual(["a", "b"]);
  structuredModelsStore.reset();
  await structuredModelsStore.revalidate("claude-api");
  await vi.advanceTimersByTimeAsync(STALE_RECHECK_MS * 3);
  expect(spy).toHaveBeenCalledTimes(3);
});

test("the list's age is the server's age plus the time since it arrived (#1503)", () => {
  const snap = {
    list: { ...list("a"), fetched_age_s: 660 },
    receivedAt: 1_000_000,
    revalidating: false,
    error: null,
  };
  expect(listAgeMinutes(snap, 1_000_000 + 60_000)).toBe(12);
  expect(listAgeMinutes({ ...snap, list: list("a") }, 1_000_000)).toBe(0);
});
