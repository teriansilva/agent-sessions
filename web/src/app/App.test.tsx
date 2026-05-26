import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ csrf: "x", new_session_engines: [], terminal_backend: "ws" }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    setTheme: vi.fn().mockResolvedValue({ theme: "royal" }),
    sessions: vi
      .fn()
      .mockResolvedValue({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } }),
    projects: vi.fn().mockResolvedValue({ projects: [] }),
  },
  setCsrfToken: vi.fn(),
  gotoChangePassword: vi.fn(),
  gotoLogin: vi.fn(),
}));

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("exposes a Settings entrypoint in both the sidebar and the mobile bar", async () => {
  render(<App />);
  // Two gears link to /settings: the sidebar .topbar one (desktop) and the .mobilebar one
  // (mobile). CSS hides one per breakpoint, but both must exist in the DOM so neither
  // surface is left without a way to reach Settings.
  const links = await screen.findAllByRole("link", { name: "Settings" });
  expect(links).toHaveLength(2);
  for (const a of links) expect(a).toHaveAttribute("href", "/settings");
  await waitFor(() => expect(links[0]).toBeInTheDocument());
});
