import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import App from "./App";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ csrf: "x", new_session_engines: [], terminal_backend: "ws" }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    setTheme: vi.fn().mockResolvedValue({ theme: "dark" }),
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

test("the command topbar carries the Settings entrypoint (#211 redux)", async () => {
  const { container } = render(<App />);
  // The command topbar has one Settings gear (the small-screen copy lives in the sidebar
  // drawer — same href — so scope the assertion to the topbar).
  await screen.findAllByRole("link", { name: "Settings" });
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  const link = within(topbar).getByRole("link", { name: "Settings" });
  expect(link).toHaveAttribute("href", "/settings");
  await waitFor(() => expect(link).toBeInTheDocument());
});

test("desktop: the single command-bar toggle collapses then re-expands the sidebar (#132/#211)", async () => {
  // jsdom has no matchMedia → isMobile defaults false → desktop path. One topbar toggle is the
  // sole collapse affordance: it collapses when expanded ("Collapse…") and expands when
  // collapsed ("Open…").
  const { container } = render(<App />);
  const app = container.querySelector(".app");
  expect(app).not.toHaveClass("collapsed");

  await userEvent.click(await screen.findByRole("button", { name: "Collapse session list" }));
  expect(app).toHaveClass("collapsed");

  await userEvent.click(screen.getByRole("button", { name: "Open session list" }));
  expect(app).not.toHaveClass("collapsed");
});

test("the command topbar carries the overview entrypoint (#139/#211)", async () => {
  const { container } = render(<App />);
  await screen.findAllByRole("link", { name: /open session overview/i });
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  const link = within(topbar).getByRole("link", { name: /open session overview/i });
  expect(link).toHaveAttribute("href", "/overview");
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
