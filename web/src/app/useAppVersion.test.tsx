import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import { useAppVersion } from "./useAppVersion";

vi.mock("../lib/api", () => ({ api: { version: vi.fn() } }));
const mockVersion = vi.mocked(api.version);

beforeEach(() => {
  mockVersion.mockReset();
});

test("first poll pins the initial version and does not flag a new version (#169)", async () => {
  mockVersion.mockResolvedValue({ version: "v1" });
  const { result } = renderHook(() => useAppVersion());
  await waitFor(() => expect(result.current.initial).toBe("v1"));
  expect(result.current.hasNewVersion).toBe(false);
});

test("polling detects a version change and latches `hasNewVersion` (#169)", async () => {
  mockVersion.mockResolvedValueOnce({ version: "v1" });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { result } = renderHook(() => useAppVersion());
    await waitFor(() => expect(result.current.initial).toBe("v1"));
    // Next poll returns a new version.
    mockVersion.mockResolvedValue({ version: "v2" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(result.current.hasNewVersion).toBe(true);
    // Subsequent polls returning v1 again don't unset it — we know the bundle is stale.
    mockVersion.mockResolvedValue({ version: "v1" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(result.current.hasNewVersion).toBe(true); // latched
  } finally {
    vi.useRealTimers();
  }
});

test("a transient /api/version failure is ignored, polling continues (#169)", async () => {
  mockVersion.mockResolvedValueOnce({ version: "v1" });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { result } = renderHook(() => useAppVersion());
    await waitFor(() => expect(result.current.initial).toBe("v1"));
    mockVersion.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(result.current.hasNewVersion).toBe(false); // tolerated
    mockVersion.mockResolvedValueOnce({ version: "v2" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(result.current.hasNewVersion).toBe(true); // recovered + caught the change
  } finally {
    vi.useRealTimers();
  }
});

test("visibilitychange triggers an immediate version poll (#169)", async () => {
  mockVersion.mockResolvedValueOnce({ version: "v1" });
  let hidden = false;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  const { result } = renderHook(() => useAppVersion());
  await waitFor(() => expect(result.current.initial).toBe("v1"));

  // Tab hides + reappears with a new server version. The poll on visibility-change
  // catches it WITHOUT waiting for the 5-minute interval to fire.
  hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  mockVersion.mockResolvedValueOnce({ version: "v2" });
  hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() => expect(result.current.hasNewVersion).toBe(true));
});
