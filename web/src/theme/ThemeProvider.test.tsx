import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { THEME_STORAGE_KEY } from "./applyTheme";
import { ThemeProvider } from "./ThemeProvider";
import { useTheme } from "./themeStore";

vi.mock("../lib/api", () => ({
  api: { setTheme: vi.fn().mockResolvedValue({ theme: "dark" }) },
}));

function Harness() {
  const { theme, setTheme } = useTheme();
  return (
    <button type="button" onClick={() => setTheme("light")}>
      {theme}
    </button>
  );
}

function renderWithConfig(config: AppConfig | null) {
  return render(
    <ConfigCtx.Provider value={config}>
      <ThemeProvider>
        <Harness />
      </ThemeProvider>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  vi.clearAllMocks();
});

test("setTheme applies to <html>, caches locally, and persists to the server", async () => {
  renderWithConfig(null);
  expect(screen.getByRole("button")).toHaveTextContent("dark"); // default

  await userEvent.click(screen.getByRole("button")); // Harness picks "light"

  expect(screen.getByRole("button")).toHaveTextContent("light");
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  expect(api.setTheme).toHaveBeenCalledWith("light");
});

test("reconciles to the server theme once config loads", async () => {
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "light",
  });
  await waitFor(() =>
    expect(document.documentElement.dataset.theme).toBe("light"),
  );
  expect(screen.getByRole("button")).toHaveTextContent("light");
});

test("an unknown server theme falls back to the default", async () => {
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "neon",
  } as AppConfig);
  await waitFor(() =>
    expect(document.documentElement.dataset.theme).toBe("dark"),
  );
});

test("a local choice wins over the server's theme on reload (#172)", async () => {
  // The user picked light here before; the server has somehow drifted to dark (silent
  // persist failure, another device, a reset). On reload the local choice must STICK —
  // no flip to dark, no localStorage overwrite.
  localStorage.setItem(THEME_STORAGE_KEY, "light");
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "dark",
  });
  // Give the reconcile effect a tick to fire (or not). Use waitFor to assert stability
  // rather than racing the effect.
  await waitFor(() =>
    expect(document.documentElement.dataset.theme).toBe("light"),
  );
  expect(screen.getByRole("button")).toHaveTextContent("light");
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light"); // NOT overwritten to dark
});

test("a malformed local value does NOT block server seeding (Hermes #173 review)", async () => {
  // A bad value (e.g. legacy theme id "neon", a typo, manual tampering) must not be treated
  // as an explicit local choice — otherwise the device gets stuck on the coerced default
  // and ignores a valid server theme that the user picked elsewhere.
  localStorage.setItem(THEME_STORAGE_KEY, "neon");
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "dark",
  });
  await waitFor(() =>
    expect(document.documentElement.dataset.theme).toBe("dark"),
  );
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark"); // bad value overwritten
});

test("first load on a fresh device still seeds from the server (#172)", async () => {
  // No local choice yet — the server's theme is what the user picked elsewhere; the seed
  // must happen so the choice follows. This is the cross-device path the fix preserves.
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull(); // beforeEach cleared it
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "light",
  });
  await waitFor(() =>
    expect(document.documentElement.dataset.theme).toBe("light"),
  );
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light"); // now cached locally
});

/** A controllable `prefers-color-scheme: dark` media query (#1494). */
function fakeDeviceScheme(dark: boolean) {
  const listeners = new Set<() => void>();
  const mq = {
    matches: dark,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  const spy = vi.spyOn(window, "matchMedia").mockImplementation(
    () => mq as unknown as MediaQueryList,
  );
  return {
    flip(next: boolean) {
      mq.matches = next;
      listeners.forEach((fn) => fn());
    },
    restore: () => spy.mockRestore(),
  };
}

function ChoiceHarness() {
  const { theme, choice, setTheme } = useTheme();
  return (
    <button type="button" onClick={() => setTheme("system")}>
      {`${choice}:${theme}`}
    </button>
  );
}

test("Match device resolves from the OS and follows it live; data-theme never says system (#1494)", async () => {
  if (!window.matchMedia) window.matchMedia = (() => ({})) as never;
  const device = fakeDeviceScheme(false);
  try {
    render(
      <ConfigCtx.Provider value={null}>
        <ThemeProvider>
          <ChoiceHarness />
        </ThemeProvider>
      </ConfigCtx.Provider>,
    );
    expect(screen.getByRole("button")).toHaveTextContent("dark:dark");

    await userEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveTextContent("system:light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
    expect(api.setTheme).toHaveBeenCalledWith("system");

    act(() => device.flip(true));
    expect(screen.getByRole("button")).toHaveTextContent("system:dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  } finally {
    device.restore();
  }
});
