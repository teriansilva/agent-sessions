import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { usePlaybookRead } from "./usePlaybooks";

test("an older successful refresh cannot clear a refusal after the latest refresh failed", async () => {
  let resolveOld!: (value: string) => void;
  const old = new Promise<string>((resolve) => {
    resolveOld = resolve;
  });
  const read = vi
    .fn()
    .mockResolvedValueOnce("first read")
    .mockReturnValueOnce(old)
    .mockRejectedValueOnce(new Error("Store unavailable"));
  const { result } = renderHook(() => usePlaybookRead(read));
  await waitFor(() => expect(result.current.data).toBe("first read"));
  let pending!: Promise<unknown>;
  act(() => {
    pending = result.current.reload();
  });
  await act(async () => {
    expect(await result.current.reload()).toBeNull();
  });
  await act(async () => {
    resolveOld("superseded read");
    expect(await pending).toBeNull();
  });
  expect(result.current.data).toBe("first read");
  expect(result.current.error).toBe("Store unavailable");
});
