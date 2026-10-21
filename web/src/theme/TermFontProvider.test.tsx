import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { TermFontProvider } from "./TermFontProvider";
import { DEFAULT_TERM_FONT_FAMILY, TERM_FONT_STORAGE_KEY } from "./termFont";
import { useTermFont } from "./termFontStore";

vi.mock("../lib/api", () => ({
  api: {
    setTermFontFamily: vi
      .fn()
      .mockResolvedValue({ term_font_family: "Menlo, monospace" }),
  },
}));

const FIRA = '"Fira Code", monospace';

function Harness({ next = "Menlo, monospace" }: { next?: string }) {
  const { family, setFamily } = useTermFont();
  return (
    <button type="button" onClick={() => setFamily(next)}>
      {family}
    </button>
  );
}

function renderWithConfig(config: AppConfig | null, next?: string) {
  return render(
    <ConfigCtx.Provider value={config}>
      <TermFontProvider>
        <Harness next={next} />
      </TermFontProvider>
    </ConfigCtx.Provider>,
  );
}

const cfg = (over: Partial<AppConfig>) => ({ csrf: "t", ...over }) as AppConfig;

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

test("setFamily applies, caches locally, and persists to the server", async () => {
  renderWithConfig(null);
  expect(screen.getByRole("button")).toHaveTextContent(
    DEFAULT_TERM_FONT_FAMILY,
  );

  await userEvent.click(screen.getByRole("button"));

  expect(screen.getByRole("button")).toHaveTextContent("Menlo, monospace");
  expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe("Menlo, monospace");
  await waitFor(() =>
    expect(api.setTermFontFamily).toHaveBeenCalledWith("Menlo, monospace"),
  );
});

test("a failed server persist still applies locally", async () => {
  vi.mocked(api.setTermFontFamily).mockRejectedValueOnce(new Error("offline"));
  renderWithConfig(null);
  await userEvent.click(screen.getByRole("button"));
  // Losing the cross-device seed must never cost the operator the face they just picked on
  // the device in front of them.
  expect(screen.getByRole("button")).toHaveTextContent("Menlo, monospace");
  expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe("Menlo, monospace");
});

test("a brand-new device seeds from the server", async () => {
  renderWithConfig(cfg({ term_font_family: FIRA }));
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent(FIRA));
  expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe(FIRA);
});

test("THE DEVICE WINS: a local choice is never overwritten by the server's", async () => {
  // This is the property that makes "one face on the phone, another on the desktop" fall out
  // of a single pref instead of needing a per-device one.
  localStorage.setItem(TERM_FONT_STORAGE_KEY, "Menlo, monospace");
  renderWithConfig(cfg({ term_font_family: FIRA }));
  await waitFor(() =>
    expect(screen.getByRole("button")).toHaveTextContent("Menlo, monospace"),
  );
  expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe("Menlo, monospace");
});

test("an INVALID local cache is not a choice — the server seed still lands", async () => {
  localStorage.setItem(TERM_FONT_STORAGE_KEY, "Menlo,,monospace"); // dead stack
  renderWithConfig(cfg({ term_font_family: FIRA }));
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent(FIRA));
  expect(localStorage.getItem(TERM_FONT_STORAGE_KEY)).toBe(FIRA);
});

test("a hostile server value is coerced, never applied", async () => {
  renderWithConfig(cfg({ term_font_family: "url(evil.woff2)" }));
  await waitFor(() =>
    expect(screen.getByRole("button")).toHaveTextContent(
      DEFAULT_TERM_FONT_FAMILY,
    ),
  );
});

test("an older server (no field) leaves the device on its own value", async () => {
  localStorage.setItem(TERM_FONT_STORAGE_KEY, FIRA);
  renderWithConfig(cfg({}));
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent(FIRA));
  expect(api.setTermFontFamily).not.toHaveBeenCalled();
});

test("server writes are SERIALIZED — the last value chosen is the last one sent", async () => {
  // Typing in the custom field can fire a write per keystroke and nothing orders independent
  // POSTs. If an intermediate value settled last, the server would hold a stack the operator
  // never finished typing — this device would look right while the next NEW device got seeded
  // wrong. At most one request is in flight; superseded values are dropped.
  let release: (() => void) | undefined;
  vi.mocked(api.setTermFontFamily).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        release = () => resolve({ term_font_family: "x" });
      }),
  );

  function Multi() {
    const { setFamily } = useTermFont();
    return (
      <button
        type="button"
        onClick={() => {
          setFamily("Menlo, monospace");
          setFamily("Consolas, monospace");
          setFamily(FIRA);
        }}
      >
        go
      </button>
    );
  }
  render(
    <ConfigCtx.Provider value={null}>
      <TermFontProvider>
        <Multi />
      </TermFontProvider>
    </ConfigCtx.Provider>,
  );

  await userEvent.click(screen.getByRole("button"));
  // Exactly one request while the first is in flight — the two later values collapsed.
  expect(api.setTermFontFamily).toHaveBeenCalledTimes(1);
  release?.();
  await waitFor(() => expect(api.setTermFontFamily).toHaveBeenCalledTimes(2));
  // The newest value, not the middle one — and the intermediate never went out at all.
  expect(api.setTermFontFamily).toHaveBeenLastCalledWith(FIRA);
  expect(api.setTermFontFamily).not.toHaveBeenCalledWith("Consolas, monospace");
});
