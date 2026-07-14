import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import App from "./App";
import { applySWUpdate } from "./swUpdate";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ csrf: "x", new_session_engines: [], terminal_backend: "ws" }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    setTheme: vi.fn().mockResolvedValue({ theme: "dark" }),
    setPrefs: vi.fn().mockResolvedValue({ session_list_order: "created_at" }),
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

// Footer version surface (#661): controllable SW state so the update-chip path is testable.
let swSwapped = false;
vi.mock("./swUpdate", () => ({
  swHasSwapped: () => swSwapped,
  onSWSwap: () => () => {},
  applySWUpdate: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  swSwapped = false;
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

// #548: the sidebar header's decorative "Sessions / SEC // 01" label row is now the sort-order
// toggle — same server-synced pref as the Settings radio (#506). The heading survives sr-only
// so the <aside> landmark keeps its accessible name.
test("sidebar header hosts the sort-order toggle; SEC // 01 is gone (#548)", async () => {
  render(<App />);
  const group = await screen.findByRole("radiogroup", { name: "Order" });
  expect(within(group).getByRole("radio", { name: "Recent" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(within(group).getByRole("radio", { name: "Created" })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  expect(screen.queryByText(/SEC \/\/ 01/)).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
});

test("sidebar: flipping to Created persists the pref, refreshes config, and refetches the list (#548)", async () => {
  // Two Onces (initial load, post-save refresh) so no persistent implementation leaks into
  // later tests — clearAllMocks resets calls, not implementations.
  vi.mocked(api.config)
    .mockResolvedValueOnce({
      csrf: "x",
      new_session_engines: [],
      terminal_backend: "ws",
      session_list_order: "recent_activity",
    })
    .mockResolvedValueOnce({
      csrf: "x",
      new_session_engines: [],
      terminal_backend: "ws",
      session_list_order: "created_at",
    });
  render(<App />);
  const created = await screen.findByRole("radio", { name: "Created" });
  await waitFor(() => expect(api.sessions).toHaveBeenCalled());
  const fetches = vi.mocked(api.sessions).mock.calls.length;

  await userEvent.click(created);
  expect(created).toHaveAttribute("aria-checked", "true"); // optimistic flip
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ session_list_order: "created_at" }),
  );
  // The save refreshes the shared config…
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(2));
  // …whose new order triggers exactly one page-0 refetch, re-sorting the list in place.
  await waitFor(() => expect(vi.mocked(api.sessions).mock.calls.length).toBe(fetches + 1));
  expect(created).toHaveAttribute("aria-checked", "true"); // reconciled, not reverted
});

test("sidebar: a failed order save snaps the toggle back to the server truth (#548)", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("boom"));
  render(<App />);
  const created = await screen.findByRole("radio", { name: "Created" });
  await userEvent.click(created);
  await waitFor(() => expect(created).toHaveAttribute("aria-checked", "false"));
  expect(screen.getByRole("radio", { name: "Recent" })).toHaveAttribute("aria-checked", "true");
  expect(api.config).toHaveBeenCalledTimes(1); // no config refresh on a failed save
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

// --- Footer version surface (#661) --------------------------------------------------------------

test("the footer shows the running version as a hud tag (#661)", async () => {
  const { container } = render(<App />);
  // Test builds are unstamped ("dev"), so the tag mirrors the server's version — the honest
  // report of what's installed. api.version is mocked to 0.0.0 above.
  const tag = await screen.findByText("V0.0.0");
  expect(tag).toHaveClass("hud-version");
  expect(container.querySelector("footer.hud-classbar")).toContainElement(tag);
  // In sync ⇒ no update chip.
  expect(screen.queryByRole("button", { name: /tap to reload/i })).not.toBeInTheDocument();
});

test("a swapped-in SW shell surfaces the tap-to-reload chip; tap applies via the SW path (#661)", async () => {
  swSwapped = true;
  render(<App />);
  const chip = await screen.findByRole("button", { name: /ready — tap to reload/i });
  await userEvent.click(chip);
  expect(vi.mocked(applySWUpdate)).toHaveBeenCalledTimes(1); // SW-aware reload, never bare
});
