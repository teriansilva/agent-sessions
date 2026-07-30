import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { AccentProvider } from "./AccentProvider";
import { useAccent } from "./accentStore";
import { ACCENT_STORAGE_KEY } from "./applyAccent";

vi.mock("../lib/api", () => ({ api: { setAccent: vi.fn().mockResolvedValue({ accent: "#c02020" }) } }));

function Harness() {
  const { accent, setAccent } = useAccent();
  return (
    <button type="button" onClick={() => setAccent("#c02020")}>
      {accent}
    </button>
  );
}

function renderWithConfig(config: AppConfig | null) {
  return render(
    <ConfigCtx.Provider value={config}>
      <AccentProvider>
        <Harness />
      </AccentProvider>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("style");
  vi.clearAllMocks();
});

test("setAccent applies inline, caches locally, normalizes, and persists to the server", async () => {
  renderWithConfig(null);
  expect(screen.getByRole("button")).toHaveTextContent("#ffb000"); // default

  await userEvent.click(screen.getByRole("button")); // Harness picks #c02020

  expect(screen.getByRole("button")).toHaveTextContent("#c02020");
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#c02020");
  expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#c02020");
  expect(api.setAccent).toHaveBeenCalledWith("#c02020");
});

test("seeds from the server on a fresh device (no local choice)", async () => {
  renderWithConfig({ csrf: "x", new_session_engines: [], terminal_backend: "ws", accent: "#3fbf6f" });
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("#3fbf6f"));
  expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#3fbf6f"); // now cached
});

test("a valid local accent wins over a stale server accent on reload", async () => {
  localStorage.setItem(ACCENT_STORAGE_KEY, "#19b6c9");
  renderWithConfig({ csrf: "x", new_session_engines: [], terminal_backend: "ws", accent: "#ffb000" });
  await waitFor(() => expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#19b6c9"));
  expect(screen.getByRole("button")).toHaveTextContent("#19b6c9");
  expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#19b6c9"); // not overwritten
});

test("a corrupt local value does NOT block server seeding", async () => {
  localStorage.setItem(ACCENT_STORAGE_KEY, "garbage");
  renderWithConfig({ csrf: "x", new_session_engines: [], terminal_backend: "ws", accent: "#3b82f6" });
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("#3b82f6"));
  expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#3b82f6"); // bad value overwritten
});
