import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import App from "./App";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ csrf: "x", new_session_engines: [], terminal_backend: "ws" }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    setTheme: vi.fn().mockResolvedValue({ theme: "royal" }),
    setSidebarView: vi.fn().mockResolvedValue({ sidebar_view: "overview" }),
    sessions: vi
      .fn()
      .mockResolvedValue({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } }),
    projects: vi.fn().mockResolvedValue({ projects: [] }),
  },
  setCsrfToken: vi.fn(),
  gotoChangePassword: vi.fn(),
  gotoLogin: vi.fn(),
}));

// Stub the lazy sidebar overview so the toggle test doesn't mount React Flow in jsdom.
vi.mock("../components/overview/SidebarOverview", () => ({
  default: () => <div data-testid="sidebar-overview">map</div>,
}));

beforeEach(() => {
  vi.clearAllMocks();
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

test("desktop: collapse via the sidebar button, re-expand via the header toggle (#132)", async () => {
  // jsdom has no matchMedia → isMobile defaults false, so this exercises the desktop path:
  // one collapse affordance at a time — the sidebar PanelLeftClose collapses, and only when
  // collapsed does the header toggle re-expand (no duplicate collapse button while expanded).
  const { container } = render(<App />);
  const app = container.querySelector(".app");
  expect(app).not.toHaveClass("collapsed");

  await userEvent.click(await screen.findByRole("button", { name: "Collapse session list" }));
  expect(app).toHaveClass("collapsed");

  await userEvent.click(screen.getByRole("button", { name: "Toggle session list" }));
  expect(app).not.toHaveClass("collapsed");
});

test("exposes an overview entrypoint in both the sidebar and the header (#139)", async () => {
  render(<App />);
  // Two links → /overview: the sidebar .topbar one (desktop expanded) and the .mobilebar one
  // (mobile + collapsed desktop). CSS shows one per state; both must exist in the DOM.
  const links = await screen.findAllByRole("link", { name: /open session overview/i });
  expect(links).toHaveLength(2);
  for (const a of links) expect(a).toHaveAttribute("href", "/overview");
});

test("sidebar List ⇄ Map toggle swaps the body and persists the choice (#139)", async () => {
  render(<App />);
  // Defaults to List → the session list shows, the overview is not mounted.
  expect(screen.queryByTestId("sidebar-overview")).not.toBeInTheDocument();

  await userEvent.click(await screen.findByRole("tab", { name: /map/i }));
  expect(api.setSidebarView).toHaveBeenCalledWith("overview");
  expect(await screen.findByTestId("sidebar-overview")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("tab", { name: /list/i }));
  expect(api.setSidebarView).toHaveBeenCalledWith("list");
  await waitFor(() => expect(screen.queryByTestId("sidebar-overview")).not.toBeInTheDocument());
});
