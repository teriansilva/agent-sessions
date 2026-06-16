import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ csrf: "x", new_session_engines: [], terminal_backend: "ws" }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    setTheme: vi.fn().mockResolvedValue({ theme: "dark" }),
    sessions: vi
      .fn()
      .mockResolvedValue({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } }),
    folders: vi.fn().mockResolvedValue({ folders: [] }),
    projectEntities: vi.fn().mockResolvedValue({ projects: [] }),
  },
  setCsrfToken: vi.fn(),
  gotoChangePassword: vi.fn(),
  gotoLogin: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  // App mounts a BrowserRouter on the real jsdom location — navigations leak across tests
  // in this file, so pin every test back to the landing route.
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  // The mobile tests below install matchMedia; the desktop tests rely on it being absent
  // (jsdom has none → isMobile defaults false). Remove it so state can't leak between tests.
  delete (window as { matchMedia?: unknown }).matchMedia;
});

// Force the ≤800px breakpoint so isMobile becomes true and the off-canvas drawer is in play.
function mockMobileViewport() {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes("max-width: 800px"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

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

// #357: the Settings links keep pointing at the canonical bare /settings entry; the route
// shell replace-redirects to the first tab, so every Settings navigation lands on a tab URL.
test("clicking the topbar Settings link lands on the first settings tab (#357)", async () => {
  const { container } = render(<App />);
  await screen.findAllByRole("link", { name: "Settings" });
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  await userEvent.click(within(topbar).getByRole("link", { name: "Settings" }));
  expect(await screen.findByRole("tab", { name: "Appearance" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(window.location.pathname).toBe("/settings/appearance");
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

// #424 Phase 1: the sidebar is list-only — the old List ⇄ Map tablist is gone and `/overview`
// is the canonical map. The session list always renders; there is no Map tab to switch to.
test("the sidebar is list-only — no List/Map view toggle (#424)", async () => {
  render(<App />);
  // The session list shell (its "New session" entrypoint) is present unconditionally.
  expect(await screen.findByRole("link", { name: /new session/i })).toBeInTheDocument();
  // The retired tablist and its Map tab no longer exist.
  expect(screen.queryByRole("tablist", { name: /sidebar view/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: /map/i })).not.toBeInTheDocument();
});

// The retired `tr-sidebar-view` pref is cleared once on mount so stale "overview" values from a
// previous build don't linger in localStorage (#424 Phase 1).
test("a stale tr-sidebar-view pref is cleared on mount (#424)", async () => {
  localStorage.setItem("tr-sidebar-view", "overview");
  render(<App />);
  await screen.findByRole("link", { name: /new session/i });
  await waitFor(() => expect(localStorage.getItem("tr-sidebar-view")).toBeNull());
});

// #283: on mobile, same-route nav targets (New session / Overview / Settings while already on
// that route) don't change location.pathname, so the route-change effect never closes the
// drawer. The shared closeMobileDrawer handler wired onto those links must close it in one tap.
// Default route in jsdom is "/", so all three of these are same-route no-ops on mount.
test.each([
  ["New session", /new session/i],
  ["Overview", /open session overview/i],
  ["Settings", /^settings$/i],
])("mobile: tapping same-route %s closes the open drawer in one tap (#283)", async (_label, name) => {
  mockMobileViewport();
  const { container } = render(<App />);
  const app = container.querySelector(".app") as HTMLElement;

  // Open the off-canvas drawer (mobile toggle drives navOpen, not the desktop collapse flag).
  await userEvent.click(await screen.findByRole("button", { name: "Open session list" }));
  expect(app).toHaveClass("navOpen");

  // Tap the same-route link — there can be two copies (topbar + in-drawer); either carries the
  // close handler, so the first is enough.
  const links = await screen.findAllByRole("link", { name });
  await userEvent.click(links[0]);

  await waitFor(() => expect(app).not.toHaveClass("navOpen"));
  // The desktop collapse flag must stay untouched (the two surfaces are independent, #128).
  expect(app).not.toHaveClass("collapsed");
});
