import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { TermSchemeProvider } from "./TermSchemeProvider";
import { TERM_SCHEME_STORAGE_KEY, useTermScheme } from "./termSchemeStore";
import { ThemeCtx } from "./themeStore";

vi.mock("../lib/api", () => ({
  api: { setTermScheme: vi.fn().mockResolvedValue({ term_scheme: "nord" }) },
}));

const CONFIG = { csrf: "x", new_session_engines: [], terminal_backend: "ws" } as AppConfig;

function Harness() {
  const { scheme, setScheme } = useTermScheme();
  return (
    <button type="button" onClick={() => setScheme("solarized")}>
      {scheme}
    </button>
  );
}

function renderWith(config: AppConfig | null, mode: "dark" | "light" = "dark") {
  return render(
    <ConfigCtx.Provider value={config}>
      <ThemeCtx.Provider value={{ theme: mode, choice: mode, setTheme: () => {} }}>
        <TermSchemeProvider>
          <Harness />
        </TermSchemeProvider>
      </ThemeCtx.Provider>
    </ConfigCtx.Provider>,
  );
}

const termBg = () => document.documentElement.style.getPropertyValue("--term-bg");

beforeEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--term-bg");
  vi.clearAllMocks();
});

test("the default leaves --term-bg to tokens.css; a chosen scheme sets it from the RESOLVED mode", async () => {
  const { rerender } = renderWith(null, "light");
  expect(screen.getByRole("button")).toHaveTextContent("battlelab");
  expect(termBg()).toBe("");

  await userEvent.click(screen.getByRole("button"));
  expect(localStorage.getItem(TERM_SCHEME_STORAGE_KEY)).toBe("solarized");
  expect(api.setTermScheme).toHaveBeenCalledWith("solarized");
  expect(termBg()).toBe("#fdf6e3"); // Solarized light

  rerender(
    <ConfigCtx.Provider value={null}>
      <ThemeCtx.Provider value={{ theme: "dark", choice: "system", setTheme: () => {} }}>
        <TermSchemeProvider>
          <Harness />
        </TermSchemeProvider>
      </ThemeCtx.Provider>
    </ConfigCtx.Provider>,
  );
  expect(termBg()).toBe("#002b36"); // the OS went dark: Solarized dark
});

test("a malformed stored scheme renders BattleLab and is replaced by the server's valid seed", async () => {
  localStorage.setItem(TERM_SCHEME_STORAGE_KEY, "{not-a-scheme");
  renderWith({ ...CONFIG, term_scheme: "nord" });
  await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("nord"));
  expect(localStorage.getItem(TERM_SCHEME_STORAGE_KEY)).toBe("nord");
});

test("a valid local choice wins over the server", async () => {
  localStorage.setItem(TERM_SCHEME_STORAGE_KEY, "dracula");
  renderWith({ ...CONFIG, term_scheme: "nord" });
  await new Promise((r) => setTimeout(r, 0));
  expect(screen.getByRole("button")).toHaveTextContent("dracula");
});

test("an unknown server value seeds the default, never a blank terminal", async () => {
  renderWith({ ...CONFIG, term_scheme: "from-the-future" });
  await waitFor(() =>
    expect(localStorage.getItem(TERM_SCHEME_STORAGE_KEY)).toBe("battlelab"),
  );
  expect(screen.getByRole("button")).toHaveTextContent("battlelab");
});

test("rapid picks are saved in order, latest wins — an older request can't settle last (#1498)", async () => {
  // Each POST is held open until the test releases it, so completion order is the test's to pick.
  const held: Array<{ value: string; release: () => void }> = [];
  vi.mocked(api.setTermScheme).mockImplementation(
    (value: string) =>
      new Promise((resolve) => held.push({ value, release: () => resolve({ term_scheme: value }) })),
  );
  function Picker() {
    const { setScheme } = useTermScheme();
    return (
      <>
        {(["nord", "gruvbox", "dracula"] as const).map((id) => (
          <button key={id} type="button" onClick={() => setScheme(id)}>
            {id}
          </button>
        ))}
      </>
    );
  }
  render(
    <ConfigCtx.Provider value={null}>
      <ThemeCtx.Provider value={{ theme: "dark", choice: "dark", setTheme: () => {} }}>
        <TermSchemeProvider>
          <Picker />
        </TermSchemeProvider>
      </ThemeCtx.Provider>
    </ConfigCtx.Provider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "nord" }));
  await userEvent.click(screen.getByRole("button", { name: "gruvbox" }));
  await userEvent.click(screen.getByRole("button", { name: "dracula" }));
  // One request in flight; the superseded middle pick is never sent.
  expect(held.map((h) => h.value)).toEqual(["nord"]);
  held[0].release();
  await waitFor(() => expect(held.map((h) => h.value)).toEqual(["nord", "dracula"]));
  held[1].release();
  await new Promise((r) => setTimeout(r, 0));
  // The LAST write the server sees is the device's current choice.
  expect(vi.mocked(api.setTermScheme).mock.calls.at(-1)).toEqual(["dracula"]);
  expect(localStorage.getItem(TERM_SCHEME_STORAGE_KEY)).toBe("dracula");
});
