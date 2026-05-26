import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { ThemeId } from "../theme/themes";
import { ThemeCtx } from "../theme/themeStore";
import { Settings } from "./Settings";

vi.mock("../lib/api", () => ({ api: { version: vi.fn(), setTheme: vi.fn() } }));

function renderSettings(theme: ThemeId = "royal") {
  const setTheme = vi.fn();
  render(
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme, setTheme }}>
        <Settings />
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  return { setTheme };
}

beforeEach(() => {
  vi.mocked(api.version).mockResolvedValue({ version: "1.2.3" });
});

test("renders the three themes, the version, and a safe coffee link", async () => {
  renderSettings();
  expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  for (const label of ["Royal", "Dark", "Light"]) {
    expect(screen.getByRole("radio", { name: new RegExp(label) })).toBeInTheDocument();
  }
  await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());

  const coffee = screen.getByRole("link", { name: /buy me a coffee/i });
  expect(coffee).toHaveAttribute("href", "https://buymeacoffee.com/teriansilva");
  expect(coffee).toHaveAttribute("target", "_blank");
  expect(coffee).toHaveAttribute("rel", "noopener noreferrer");
});

test("the active theme is marked aria-checked", async () => {
  renderSettings("light");
  expect(screen.getByRole("radio", { name: /Light/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: /Royal/ })).toHaveAttribute("aria-checked", "false");
  // flush the pending version fetch so its state update doesn't warn outside act()
  await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
});

test("picking a theme calls setTheme with its id", async () => {
  const { setTheme } = renderSettings("royal");
  await userEvent.click(screen.getByRole("radio", { name: /Dark/ }));
  expect(setTheme).toHaveBeenCalledWith("dark");
  await waitFor(() => expect(screen.getByText("1.2.3")).toBeInTheDocument());
});
