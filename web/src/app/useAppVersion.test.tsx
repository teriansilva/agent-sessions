import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import { useAppVersion } from "./useAppVersion";
import { applySWUpdate, onSWSwap, swHasSwapped } from "./swUpdate";

vi.mock("../lib/api", () => ({ api: { version: vi.fn() } }));
vi.mock("./swUpdate", () => ({
  swHasSwapped: vi.fn(() => false),
  onSWSwap: vi.fn(() => () => {}),
  applySWUpdate: vi.fn(),
}));
const mockVersion = vi.mocked(api.version);
const mockSwapped = vi.mocked(swHasSwapped);
const mockOnSwap = vi.mocked(onSWSwap);

beforeEach(() => {
  mockVersion.mockReset();
  mockSwapped.mockReset().mockReturnValue(false);
  mockOnSwap.mockReset().mockReturnValue(() => {});
});

test("a stamped build that matches the server shows the version and no update (#661)", async () => {
  mockVersion.mockResolvedValue({ version: "1.0.0" });
  const { result } = renderHook(() => useAppVersion("1.0.0"));
  await waitFor(() => expect(result.current.server).toBe("1.0.0"));
  expect(result.current.displayVersion).toBe("1.0.0");
  expect(result.current.updateReady).toBe(false);
});

test("server ahead of the build stamp flags the update (#661 — the honest baseline)", async () => {
  // The pre-#661 baseline was the FIRST /api/version response, which can't prove what
  // bundle this tab loaded. The stamp can: server 1.0.1 vs stamped 1.0.0 ⇒ stale tab.
  mockVersion.mockResolvedValue({ version: "1.0.1" });
  const { result } = renderHook(() => useAppVersion("1.0.0"));
  await waitFor(() => expect(result.current.updateReady).toBe(true));
  expect(result.current.displayVersion).toBe("1.0.0"); // what THIS tab runs, not the server
  expect(result.current.server).toBe("1.0.1");
});

test("an unstamped (dev) build never claims to be stale — it shows the server's version (#661)", async () => {
  mockVersion.mockResolvedValue({ version: "9.9.9" });
  const { result } = renderHook(() => useAppVersion("dev"));
  await waitFor(() => expect(result.current.server).toBe("9.9.9"));
  expect(result.current.updateReady).toBe(false); // "dev" disables the mismatch path
  expect(result.current.displayVersion).toBe("9.9.9"); // honest: report what the server runs
});

test("a transient /api/version failure is ignored, polling continues (#169)", async () => {
  mockVersion.mockRejectedValueOnce(new Error("network down"));
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { result } = renderHook(() => useAppVersion("1.0.0"));
    expect(result.current.server).toBeNull(); // tolerated
    mockVersion.mockResolvedValue({ version: "1.0.1" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(result.current.updateReady).toBe(true); // recovered + caught the change
  } finally {
    vi.useRealTimers();
  }
});

test("visibilitychange triggers an immediate version poll (#169)", async () => {
  mockVersion.mockResolvedValueOnce({ version: "1.0.0" });
  let hidden = false;
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get: () => hidden,
  });
  const { result } = renderHook(() => useAppVersion("1.0.0"));
  await waitFor(() => expect(result.current.server).toBe("1.0.0"));

  // Tab hides + reappears after a deploy. The visibility poll catches it WITHOUT
  // waiting for the 5-minute interval to fire.
  hidden = true;
  document.dispatchEvent(new Event("visibilitychange"));
  mockVersion.mockResolvedValueOnce({ version: "1.0.1" });
  hidden = false;
  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() => expect(result.current.updateReady).toBe(true));
});

test("a service-worker shell swap flags the update even when versions agree (#661)", async () => {
  mockVersion.mockResolvedValue({ version: "1.0.0" });
  let fireSwap: (() => void) | undefined;
  mockOnSwap.mockImplementation((cb) => {
    fireSwap = cb;
    return () => {};
  });
  const { result } = renderHook(() => useAppVersion("1.0.0"));
  await waitFor(() => expect(result.current.server).toBe("1.0.0"));
  expect(result.current.updateReady).toBe(false);
  act(() => fireSwap?.());
  expect(result.current.updateReady).toBe(true); // fresh shell already precached — offer it
});

test("applyUpdate delegates to the SW update path, not a bare reload (#661)", async () => {
  mockVersion.mockResolvedValue({ version: "1.0.1" });
  const { result } = renderHook(() => useAppVersion("1.0.0"));
  await waitFor(() => expect(result.current.updateReady).toBe(true));
  result.current.applyUpdate();
  expect(vi.mocked(applySWUpdate)).toHaveBeenCalledTimes(1);
});
