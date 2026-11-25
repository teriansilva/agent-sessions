import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { TermSizeProvider } from "./TermSizeProvider";
import { DEFAULT_TERM_FONT_SIZE, TERM_SIZE_STORAGE_KEY } from "./termSize";
import { useTermSize } from "./termSizeStore";

vi.mock("../lib/api", () => ({
  api: { setTermFontSize: vi.fn().mockResolvedValue({ term_font_size: 10 }) },
}));

function Harness() {
  const { size, setSize } = useTermSize();
  return (
    <button type="button" onClick={() => setSize(10)}>
      {String(size)}
    </button>
  );
}

function renderWithConfig(config: AppConfig | null) {
  return render(
    <ConfigCtx.Provider value={config}>
      <TermSizeProvider>
        <Harness />
      </TermSizeProvider>
    </ConfigCtx.Provider>,
  );
}

const cfg = (over: Partial<AppConfig>) => ({ csrf: "t", ...over }) as AppConfig;

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

test("setSize applies, caches locally, and persists to the server", async () => {
  renderWithConfig(null);
  expect(screen.getByRole("button")).toHaveTextContent(
    String(DEFAULT_TERM_FONT_SIZE),
  );

  await userEvent.click(screen.getByRole("button"));

  expect(screen.getByRole("button")).toHaveTextContent("10");
  expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe("10");
  await waitFor(() => expect(api.setTermFontSize).toHaveBeenCalledWith(10));
});

test("a failed server persist still applies locally", async () => {
  vi.mocked(api.setTermFontSize).mockRejectedValueOnce(new Error("offline"));
  renderWithConfig(null);
  await userEvent.click(screen.getByRole("button"));
  // The size is a display preference: losing the cross-device seed must never cost the
  // operator the zoom they just asked for on the device in front of them.
  expect(screen.getByRole("button")).toHaveTextContent("10");
  expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe("10");
});

test("a brand-new device seeds from the server", async () => {
  renderWithConfig(cfg({ term_font_size: 9 }));
  await waitFor(() =>
    expect(screen.getByRole("button")).toHaveTextContent("9"),
  );
  expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe("9");
});

test("a valid local choice WINS over the server value on reload", async () => {
  // This is the whole per-device property (#859): a phone parked at 10px must not be
  // dragged back to 13px every reload because the desktop wrote 13 to the server last.
  localStorage.setItem(TERM_SIZE_STORAGE_KEY, "10");
  renderWithConfig(cfg({ term_font_size: 13 }));
  expect(screen.getByRole("button")).toHaveTextContent("10");
  await waitFor(() =>
    expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe("10"),
  );
  expect(screen.getByRole("button")).toHaveTextContent("10");
});

test("an INVALID local cache does not out-rank the server", async () => {
  // A clamped-but-unusable cached value is not a choice. If it out-ranked the server, a
  // corrupt cache would pin the device forever.
  localStorage.setItem(TERM_SIZE_STORAGE_KEY, "2");
  renderWithConfig(cfg({ term_font_size: 9 }));
  await waitFor(() =>
    expect(screen.getByRole("button")).toHaveTextContent("9"),
  );
  expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBe("9");
});

test("an older server without the field leaves the local default alone", async () => {
  renderWithConfig(cfg({}));
  expect(screen.getByRole("button")).toHaveTextContent(
    String(DEFAULT_TERM_FONT_SIZE),
  );
  // No seed happened, so nothing was cached — the device is still free to inherit later.
  expect(localStorage.getItem(TERM_SIZE_STORAGE_KEY)).toBeNull();
});

test("a hostile server value is clamped, not applied raw", async () => {
  renderWithConfig(cfg({ term_font_size: 2 }));
  await waitFor(() =>
    expect(screen.getByRole("button")).toHaveTextContent("8"),
  );
});

test("rapid steps serialize: one request in flight, and the LAST value is the last written", async () => {
  // #859 review: firing a POST per tap leaves the server's final value to the network. Stepping
  // 13 -> 9 sent four concurrent writes, and if the 12 px one settled last the server held 12
  // while this device showed 9 — the device looks right and the next NEW device is seeded wrong.
  const settle: ((v: unknown) => void)[] = [];
  vi.mocked(api.setTermFontSize).mockImplementation(
    () => new Promise((resolve) => settle.push(resolve)),
  );

  function Stepper() {
    const { size, setSize } = useTermSize();
    return (
      <button type="button" onClick={() => setSize(size - 1)}>
        {String(size)}
      </button>
    );
  }
  render(
    <ConfigCtx.Provider value={null}>
      <TermSizeProvider>
        <Stepper />
      </TermSizeProvider>
    </ConfigCtx.Provider>,
  );
  const btn = screen.getByRole("button");

  for (let i = 0; i < 4; i++) await userEvent.click(btn); // 13 -> 9
  expect(btn).toHaveTextContent("9");

  // Exactly ONE request went out, carrying the first step; the rest are queued behind it.
  expect(api.setTermFontSize).toHaveBeenCalledTimes(1);
  expect(api.setTermFontSize).toHaveBeenLastCalledWith(12);

  settle[0]?.({});
  // Draining sends the LATEST queued value, not each intermediate one.
  await waitFor(() => expect(api.setTermFontSize).toHaveBeenCalledTimes(2));
  expect(api.setTermFontSize).toHaveBeenLastCalledWith(9);

  settle[1]?.({});
  await waitFor(() => expect(api.setTermFontSize).toHaveBeenCalledTimes(2));
  // The final write the server sees is the size the operator actually chose.
  expect(api.setTermFontSize).toHaveBeenLastCalledWith(9);
});

test("a rejected write does not wedge the queue", async () => {
  // The drain must keep going after a failure, or one offline blip strands every later change.
  vi.mocked(api.setTermFontSize)
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue({ term_font_size: 11 });

  function Stepper() {
    const { size, setSize } = useTermSize();
    return (
      <button type="button" onClick={() => setSize(size - 1)}>
        {String(size)}
      </button>
    );
  }
  render(
    <ConfigCtx.Provider value={null}>
      <TermSizeProvider>
        <Stepper />
      </TermSizeProvider>
    </ConfigCtx.Provider>,
  );
  const btn = screen.getByRole("button");
  await userEvent.click(btn); // 12 — this one rejects
  await userEvent.click(btn); // 11 — must still be attempted
  await waitFor(() => expect(api.setTermFontSize).toHaveBeenLastCalledWith(11));
  expect(btn).toHaveTextContent("11");
});
