import { afterEach, describe, expect, it, vi } from "vitest";
import { shareLink } from "./shareLink";

const nav = navigator as Navigator & { share?: unknown; canShare?: unknown };

afterEach(() => {
  delete nav.share;
  delete nav.canShare;
  vi.restoreAllMocks();
});

function mockClipboard(write: (s: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn(write) },
  });
  return navigator.clipboard.writeText as ReturnType<typeof vi.fn>;
}

describe("shareLink (#1232)", () => {
  it("uses the share sheet with an absolute URL on this origin", async () => {
    const share = vi.fn(async () => {});
    nav.share = share;
    expect(await shareLink({ title: "T", path: "/s/claude/abc" })).toBe("shared");
    expect(share).toHaveBeenCalledWith({
      title: "T",
      url: `${window.location.origin}/s/claude/abc`,
    });
  });

  it("a dismissed sheet is an answer: no clipboard fallback", async () => {
    nav.share = vi.fn(async () => {
      throw new DOMException("no", "AbortError");
    });
    const write = mockClipboard(async () => {});
    expect(await shareLink({ title: "T", path: "/s/claude/abc" })).toBe("dismissed");
    expect(write).not.toHaveBeenCalled();
  });

  it("copies when there is no share sheet", async () => {
    const write = mockClipboard(async () => {});
    expect(await shareLink({ title: "T", path: "/mission?m=x" })).toBe("copied");
    expect(write).toHaveBeenCalledWith(`${window.location.origin}/mission?m=x`);
  });

  it("reports a refused clipboard write as failed", async () => {
    mockClipboard(async () => {
      throw new Error("insecure");
    });
    expect(await shareLink({ title: "T", path: "/s/claude/abc" })).toBe("failed");
  });
});
