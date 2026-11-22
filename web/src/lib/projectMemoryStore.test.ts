import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "./api";
import {
  beginMemoryRead,
  publishProjectMemory,
  resetProjectMemoryStore,
  setProjectMemory,
  useProjectMemory,
} from "./projectMemoryStore";

vi.mock("./api", async (orig) => {
  const actual = await orig<typeof import("./api")>();
  return { ...actual, api: { memoryProjects: vi.fn(), setMemoryEnabled: vi.fn() } };
});

const row = (id: string, enabled: boolean) => ({
  id,
  name: id,
  color: "",
  enabled,
  notes: 0,
  pending: 0,
  quarantined: 0,
  last_write: null,
});

beforeEach(() => {
  vi.clearAllMocks();
  resetProjectMemoryStore();
});

test("one flag per project, shared by every reader", () => {
  const a = renderHook(() => useProjectMemory("p-1"));
  const b = renderHook(() => useProjectMemory("p-1"));
  expect(a.result.current).toBeUndefined();
  act(() => publishProjectMemory("p-1", true));
  expect(a.result.current).toBe(true);
  expect(b.result.current).toBe(true);
});

test("a late answer never overwrites a newer one for the same project", () => {
  const hook = renderHook(() => useProjectMemory("p-1"));
  const older = beginMemoryRead();
  const newer = beginMemoryRead();
  act(() => publishProjectMemory("p-1", false, newer));
  act(() => publishProjectMemory("p-1", true, older));
  expect(hook.result.current).toBe(false);
});

test("a late answer for a project you have left lands in ITS slot only", async () => {
  let settle: (v: { enabled: boolean }) => void = () => {};
  vi.mocked(api.setMemoryEnabled).mockImplementation(() => new Promise((r) => (settle = r)));
  act(() => publishProjectMemory("p-2", false));
  const p1 = renderHook(() => useProjectMemory("p-1"));
  const p2 = renderHook(() => useProjectMemory("p-2"));
  const pending = setProjectMemory("p-1", true);
  await act(async () => {
    settle({ enabled: true });
    await pending;
  });
  expect(p1.result.current).toBe(true);
  expect(p2.result.current).toBe(false);
});

test("a failed save re-reads the server and publishes what is stored, then rethrows", async () => {
  act(() => publishProjectMemory("p-1", true));
  vi.mocked(api.setMemoryEnabled).mockRejectedValue(new ApiError(500, "disk full"));
  vi.mocked(api.memoryProjects).mockResolvedValue({ projects: [row("p-1", true)] });
  const hook = renderHook(() => useProjectMemory("p-1"));
  await act(async () => {
    await expect(setProjectMemory("p-1", false)).rejects.toThrow("disk full");
  });
  expect(api.memoryProjects).toHaveBeenCalled();
  expect(hook.result.current).toBe(true);
});
