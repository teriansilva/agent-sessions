import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { busyDelayMs, BUSY_TRIES, phaseForProbe } from "./mediaPreviewLogic";

// The two transports are decided by `isApiTunneled()`; each test sets it.
let tunneled = false;
vi.mock("../../lib/api", () => {
  class ApiError extends Error {
    status: number;
    record?: unknown;
    constructor(status: number, message: string, record?: unknown) {
      super(message);
      this.status = status;
      this.record = record;
    }
  }
  return {
    ApiError,
    FILE_BLOB_CAP: 25 * 1024 * 1024,
    isApiTunneled: () => tunneled,
    filesRawUrl: (p: string, download = false, attempt = 0) =>
      `/api/files/raw?path=${encodeURIComponent(p)}${download ? "&download=1" : ""}${attempt > 0 ? `&attempt=${attempt}` : ""}`,
    api: { fileBlob: vi.fn(), filesRawProbe: vi.fn() },
  };
});
const { api, ApiError } = await import("../../lib/api");
const { MediaPreview, NotPreviewable } = await import("./MediaPreview");

const fileBlob = vi.mocked(api.fileBlob);
const probe = vi.mocked(api.filesRawProbe);

beforeEach(() => {
  tunneled = false;
  fileBlob.mockReset();
  probe.mockReset();
  globalThis.URL.createObjectURL = vi.fn(() => "blob:media-1");
  globalThis.URL.revokeObjectURL = vi.fn();
});
afterEach(() => vi.useRealTimers());

describe("the status probe (pure)", () => {
  test("503 is busy, 200/206 is a decode failure, anything else says what the read would", () => {
    expect(phaseForProbe({ status: 503, retryAfter: 3, detail: "" }, 0)).toEqual({
      k: "busy",
      tries: 0,
      waitS: 3,
    });
    expect(phaseForProbe({ status: 206, retryAfter: null, detail: "" }, 0)).toEqual({ k: "cantplay" });
    expect(phaseForProbe({ status: 200, retryAfter: null, detail: "" }, 0)).toEqual({ k: "cantplay" });
    expect(phaseForProbe({ status: 404, retryAfter: null, detail: "no such file or directory" }, 0)).toEqual({
      k: "unavailable",
      message: "no such file or directory",
    });
  });

  test("busy retries back off from Retry-After", () => {
    expect([0, 1, 2].map((t) => busyDelayMs(3, t))).toEqual([3000, 6000, 12000]);
    expect(busyDelayMs(null, 0)).toBe(3000);
  });
});

describe("direct connection", () => {
  test("the element streams from the raw URL and Download is a plain link", () => {
    const { container } = render(
      <MediaPreview path="/home/u/a.webm" name="a.webm" size={2048} mime="video/webm" kind="video" />,
    );
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toBe("/api/files/raw?path=%2Fhome%2Fu%2Fa.webm");
    const dl = container.querySelector("a[data-media-download]")!;
    expect(dl.getAttribute("href")).toBe("/api/files/raw?path=%2Fhome%2Fu%2Fa.webm&download=1");
    expect(fileBlob).not.toHaveBeenCalled();
  });

  test("a decode failure is re-requested once when reachable, then says Can't play here", async () => {
    probe.mockResolvedValue({ status: 206, retryAfter: null, detail: "" });
    const { container } = render(
      <MediaPreview path="/home/u/a.mp4" name="a.mp4" size={10} mime="video/mp4" kind="video" />,
    );
    fireEvent.error(container.querySelector("video")!);
    await waitFor(() =>
      expect(container.querySelector("video")!.getAttribute("src")).toContain("attempt=1"),
    );
    expect(screen.queryByText("Viewer // Can't play here")).toBeNull();
    fireEvent.error(container.querySelector("video")!);
    await screen.findByText("Viewer // Can't play here");
    expect(probe).toHaveBeenCalledTimes(2);
  });

  test("503 on the element, a free slot by the probe: it is re-requested, never Can't play here", async () => {
    // Hermes 6644: the slot that refused the element frees before the probe, which sees 206.
    probe.mockResolvedValueOnce({ status: 206, retryAfter: null, detail: "" });
    const { container } = render(
      <MediaPreview path="/home/u/a.webm" name="a.webm" size={10} mime="video/webm" kind="video" />,
    );
    fireEvent.error(container.querySelector("video")!);
    await waitFor(() =>
      expect(container.querySelector("video")!.getAttribute("src")).toContain("attempt=1"),
    );
    fireEvent.loadedMetadata(container.querySelector("video")!);
    expect(screen.queryByText("Viewer // Can't play here")).toBeNull();
    expect(container.querySelector("[data-media-preview]")!.getAttribute("data-media-phase")).toBe("ready");
  });

  test("a vanished file says the read's message, not Can't play here", async () => {
    probe.mockResolvedValue({ status: 404, retryAfter: null, detail: "no such file or directory" });
    const { container } = render(
      <MediaPreview path="/home/u/a.png" name="a.png" size={10} mime="image/png" kind="image" />,
    );
    fireEvent.error(container.querySelector("img")!);
    await screen.findByText("no such file or directory");
    expect(screen.queryByText("Viewer // Can't play here")).toBeNull();
  });

  test("busy retries on its own after Retry-After, with a new request, and stops after the cap", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    probe.mockResolvedValue({ status: 503, retryAfter: 1, detail: "" });
    const { container } = render(
      <MediaPreview path="/home/u/a.webm" name="a.webm" size={10} mime="video/webm" kind="video" />,
    );
    for (let i = 0; i < BUSY_TRIES; i++) {
      fireEvent.error(container.querySelector("video")!);
      await screen.findByText("Viewer // Busy");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(busyDelayMs(1, i));
      });
      expect(container.querySelector("video")!.getAttribute("src")).toContain(`attempt=${i + 1}`);
    }
    fireEvent.error(container.querySelector("video")!);
    await screen.findByText(/Try again when one finishes/);
    expect(screen.getByRole("button", { name: /Retry now/ })).toBeTruthy();
  });

  test("closing the viewer cancels a pending busy retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    probe.mockResolvedValue({ status: 503, retryAfter: 1, detail: "" });
    const { container, unmount } = render(
      <MediaPreview path="/home/u/a.webm" name="a.webm" size={10} mime="video/webm" kind="video" />,
    );
    fireEvent.error(container.querySelector("video")!);
    await screen.findByText("Viewer // Busy");
    const clear = vi.spyOn(window, "clearTimeout");
    unmount();
    expect(clear).toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(probe).toHaveBeenCalledTimes(1);
  });
});

describe("Home Free app mode", () => {
  test("bytes come through the seam into an object URL; no /api URL reaches the element", async () => {
    tunneled = true;
    fileBlob.mockResolvedValue(new Blob(["x"], { type: "image/png" }));
    const { container } = render(
      <MediaPreview path="/home/u/a.png" name="a.png" size={10} mime="image/png" kind="image" />,
    );
    await waitFor(() => expect(container.querySelector("img")).not.toBeNull());
    expect(container.querySelector("img")!.getAttribute("src")).toBe("blob:media-1");
    expect(container.innerHTML).not.toContain("/api/files/raw");
    expect(fileBlob).toHaveBeenCalledWith("/home/u/a.png", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  test("over the cap: no fetch, Too large to preview, Download disabled with its reason", () => {
    tunneled = true;
    render(
      <MediaPreview path="/home/u/big.webm" name="big.webm" size={26 * 1024 * 1024} mime="video/webm" kind="video" />,
    );
    expect(screen.getByText("Viewer // Home Free // Too large to preview")).toBeTruthy();
    const dl = screen.getByRole("button", { name: /Download/ });
    expect((dl as HTMLButtonElement).disabled).toBe(true);
    expect(dl.getAttribute("title")).toMatch(/box's own address/);
    expect(fileBlob).not.toHaveBeenCalled();
  });

  test("a tunnel that gives up shows Timed out with Retry, and Retry fetches again", async () => {
    tunneled = true;
    fileBlob.mockRejectedValueOnce(new TypeError("network")).mockResolvedValueOnce(new Blob(["x"]));
    const { container } = render(
      <MediaPreview path="/home/u/a.ogg" name="a.ogg" size={10} mime="audio/ogg" kind="audio" />,
    );
    await screen.findByText("Viewer // Home Free // Timed out");
    fireEvent.click(screen.getByRole("button", { name: /^Retry$/ }));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
    expect(fileBlob).toHaveBeenCalledTimes(2);
  });

  test("a tunnelled 503 is busy, not timed out", async () => {
    tunneled = true;
    fileBlob.mockRejectedValue(new ApiError(503, "busy", { retryAfter: 3 }));
    render(<MediaPreview path="/home/u/a.ogg" name="a.ogg" size={10} mime="audio/ogg" kind="audio" />);
    await screen.findByText("Viewer // Busy");
  });
});

test("a binary the browser cannot show says so and offers Download", () => {
  const { container } = render(
    <NotPreviewable path="/home/u/c.mkv" name="c.mkv" size={2048} mime="video/x-matroska" />,
  );
  expect(screen.getByText("Viewer // Not previewable")).toBeTruthy();
  expect(container.querySelector("a[data-media-download]")!.getAttribute("href")).toContain("download=1");
});
