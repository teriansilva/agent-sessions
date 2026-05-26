import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { THEME_STORAGE_KEY } from "./applyTheme";
import { ThemeProvider } from "./ThemeProvider";
import { useTheme } from "./themeStore";

vi.mock("../lib/api", () => ({ api: { setTheme: vi.fn().mockResolvedValue({ theme: "dark" }) } }));

function Harness() {
  const { theme, setTheme } = useTheme();
  return (
    <button type="button" onClick={() => setTheme("dark")}>
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
  expect(screen.getByRole("button")).toHaveTextContent("royal");

  await userEvent.click(screen.getByRole("button"));

  expect(screen.getByRole("button")).toHaveTextContent("dark");
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  expect(api.setTheme).toHaveBeenCalledWith("dark");
});

test("reconciles to the server theme once config loads", async () => {
  renderWithConfig({ csrf: "x", new_session_engines: [], terminal_backend: "ws", theme: "light" });
  await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
  expect(screen.getByRole("button")).toHaveTextContent("light");
});

test("an unknown server theme falls back to the default", async () => {
  renderWithConfig({
    csrf: "x",
    new_session_engines: [],
    terminal_backend: "ws",
    theme: "neon",
  } as AppConfig);
  await waitFor(() => expect(document.documentElement.dataset.theme).toBe("royal"));
});
