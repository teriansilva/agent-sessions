import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import App from "./App";
import { applySWUpdate } from "./swUpdate";

// Mock the whole API surface the shell touches on load so render is deterministic.
vi.mock("../lib/api", () => ({
  api: {
    // The engine roster (#853 P4) — the generated fixture, as the server would serve it.
    engines: vi
      .fn()
      .mockImplementation(async () => (await import("../test/roster.fixture.json")).default),
    config: vi
      .fn()
      .mockResolvedValue({
        csrf: "x",
        new_session_engines: [],
        terminal_backend: "ws",
        // The operator tile renders once the auth mode is known (#1058); Settings and Help live
        // in its menu since #1085.
        auth_mode: "single-user",
        username: "marcus",
      }),
    version: vi.fn().mockResolvedValue({ version: "0.0.0" }),
    // #726 Phase 3: the topbar mounts the notification bell, which polls on mount.
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    markNotificationsRead: vi
      .fn()
      .mockResolvedValue({ notifications: [], unread: 0, marked: 0 }),
    setTheme: vi.fn().mockResolvedValue({ theme: "dark" }),
    setPrefs: vi.fn().mockResolvedValue({ session_list_order: "created_at" }),
    sessions: vi
      .fn()
      .mockResolvedValue({
        sessions: [],
        next_offset: null,
        total: 0,
        facets: { projects: [], engines: [] },
      }),
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

/** Open the operator tile's menu and return it. */
async function openOperatorMenu() {
  await userEvent.click(await screen.findByTestId("operator-menu"));
  return screen.findByTestId("operator-menu-panel");
}

test("Settings and Help live in the operator menu, not as their own topbar icons (#1085)", async () => {
  const { container } = render(<App />);
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  await screen.findByTestId("operator-menu");
  // No ⚙ link and no `?` button of their own any more — in the bar or in the drawer.
  expect(within(topbar).queryByRole("link", { name: "Settings" })).toBeNull();
  expect(within(topbar).queryByRole("button", { name: "Help" })).toBeNull();
  expect(container.querySelector(".sidebar-actions")).toBeNull();
  // …and no SYS clock / MISSION timer readout either.
  expect(topbar.textContent).not.toMatch(/SYS \/\/|MISSION \/\//);
  const menu = await openOperatorMenu();
  expect(
    within(menu)
      .getAllByRole("menuitem")
      .map((n) => n.textContent?.replace(/\s+/g, " ").trim()),
  ).toEqual(
    expect.arrayContaining(["Settings", "Security & 2FA", "Intro tour", "Sign out"]),
  );
  expect(within(menu).getByRole("menuitem", { name: "Settings" })).toHaveAttribute(
    "href",
    "/settings",
  );
  expect(
    within(menu).getByRole("menuitem", { name: "Documentation (opens in a new tab)" }),
  ).toHaveAttribute("target", "_blank");
});

// #357/#956: the Settings links keep pointing at the canonical bare /settings entry; on desktop
// the route shell replace-redirects to the first section, so every Settings navigation lands on
// a section URL.
test("choosing Settings in the operator menu lands on the first settings section (#357, #1085)", async () => {
  render(<App />);
  const menu = await openOperatorMenu();
  await userEvent.click(within(menu).getByRole("menuitem", { name: "Settings" }));
  expect(
    await screen.findByRole("link", { name: "Appearance" }),
  ).toHaveAttribute("aria-current", "page");
  expect(window.location.pathname).toBe("/settings/appearance");
});

test("the bottom bar carries no host-wide agent count — the dashboard's scoped one is the count", async () => {
  const { container } = render(<App />);
  const bar = container.querySelector("footer.hud-classbar") as HTMLElement;
  expect(within(bar).queryByTestId("agent-counts")).toBeNull();
  expect(bar).not.toHaveTextContent(/AGENTS LIVE/);
});

test("desktop: the single command-bar toggle collapses then re-expands the sidebar (#132/#211)", async () => {
  // jsdom has no matchMedia → isMobile defaults false → desktop path. One topbar toggle is the
  // sole collapse affordance: it collapses when expanded ("Collapse…") and expands when
  // collapsed ("Open…").
  const { container } = render(<App />);
  const app = container.querySelector(".app");
  expect(app).not.toHaveClass("collapsed");

  await userEvent.click(
    await screen.findByRole("button", { name: "Collapse session list" }),
  );
  expect(app).toHaveClass("collapsed");

  await userEvent.click(
    screen.getByRole("button", { name: "Open session list" }),
  );
  expect(app).not.toHaveClass("collapsed");
});

test("the command topbar names every work section, Library last, the map under Sessions (#1058, #1069, #1294)", async () => {
  // The map and the template gallery used to be unlabelled `.gear` icons in the action cluster,
  // beside Help and Settings, which are not destinations. Every destination is in the nav now —
  // and the ORDER is asserted, because "it contains four links" would pass on a nav that
  // shuffles itself on every render. Since #1069 Ask leads and the map is a Sessions sub-menu
  // entry, so it is NOT a top-level link: the menu is closed here.
  const { container } = render(<App />);
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  const nav = await within(topbar).findByRole("navigation", {
    name: "Main sections",
  });
  expect(
    within(nav)
      .getAllByRole("link")
      .map((a) => [
        a.querySelector(".section-nav-label")?.textContent,
        a.getAttribute("href"),
      ]),
  ).toEqual([
    ["Dashboard", "/dashboard"],
    ["Sessions", "/"],
    ["Missions", "/mission"],
    ["Library", "/templates"],
  ]);
  // The map is one click away behind the Sessions chevron.
  await userEvent.click(
    within(nav).getByRole("button", { name: "Sessions menu" }),
  );
  const menu = await screen.findByRole("menu", { name: "Sessions menu" });
  expect(
    within(menu)
      .getAllByRole("menuitem")
      .map((a) => [a.textContent, a.getAttribute("href")]),
  ).toEqual([
    ["Sessions", "/"],
    ["Sessions map", "/overview"],
  ]);
  // …and the cluster beside them is actions ONLY. An `/overview` or `/templates` link outside the
  // nav would mean the old duplication came back.
  const actions = topbar.querySelector(".hud-topbar-actions") as HTMLElement;
  expect(actions.querySelector('a[href="/overview"]')).toBeNull();
  expect(actions.querySelector('a[href="/templates"]')).toBeNull();
  expect(within(actions).getByTestId("operator-menu")).toBeInTheDocument();
});

test("Ask is the icon directly beside the bell, and it opens the right-hand sidebar (#1294)", async () => {
  const { container } = render(<App />);
  const topbar = container.querySelector(".hud-topbar") as HTMLElement;
  const ask = await within(topbar).findByTestId("ask-toggle");
  const bell = within(topbar).getByRole("button", { name: /^Notifications/ });
  // Neighbours: the Ask icon's wrapper is the bell wrapper's previous sibling.
  expect(ask.parentElement?.nextElementSibling).toBe(bell.parentElement);
  expect(ask).toHaveAttribute("aria-expanded", "false");
  // Not mounted until first asked for: no stream, no NEEDS YOU poll on a closed panel.
  expect(screen.queryByTestId("ask-sidebar")).toBeNull();
  await userEvent.click(ask);
  const panel = await screen.findByTestId("ask-sidebar");
  expect(panel).toHaveAttribute("data-open", "true");
  expect(ask).toHaveAttribute("aria-expanded", "true");
  expect(within(panel).getByRole("heading", { name: "Ask" })).toBeInTheDocument();
  // Closing hides it but keeps it MOUNTED — that is what keeps a conversation (module note).
  await userEvent.click(within(panel).getByRole("button", { name: "Close Ask" }));
  expect(screen.getByTestId("ask-sidebar")).toHaveAttribute("data-open", "false");
  expect(ask).toHaveAttribute("aria-expanded", "false");
});

test("the section labels stay in the DOM, so an icon-only bar is still named (#1058)", async () => {
  // At ≤640px `App.css` CLIPS the labels rather than removing them. A link whose text is gone has
  // no accessible name, and five unnamed icons is the row a screen-reader operator cannot use.
  // jsdom applies no media queries, so this asserts the invariant the CSS relies on: the name is
  // the link's own text, never an `aria-label` that a future edit could let drift from it.
  const { container } = render(<App />);
  const nav = await within(
    container.querySelector(".hud-topbar") as HTMLElement,
  ).findByRole("navigation", { name: "Main sections" });
  for (const a of within(nav).getAllByRole("link")) {
    expect(a).not.toHaveAttribute("aria-label");
    // The accessible name IS the label's text — decorations like the BETA tag (#1085) are
    // aria-hidden and do not join it.
    const label = a.querySelector(".section-nav-label")?.textContent ?? "";
    expect(label).not.toBe("");
    expect(within(nav).getByRole("link", { name: label })).toBe(a);
  }
});

// #424 Phase 1: the sidebar is list-only — the old List ⇄ Map tablist is gone and `/overview`
// is the canonical map. The session list always renders; there is no Map tab to switch to.
test("the sidebar is list-only — no List/Map view toggle (#424)", async () => {
  render(<App />);
  // The session list shell (its "New session" entrypoint) is present unconditionally.
  expect(
    await screen.findByRole("link", { name: /new session/i }),
  ).toBeInTheDocument();
  // The retired tablist and its Map tab no longer exist.
  expect(
    screen.queryByRole("tablist", { name: /sidebar view/i }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("tab", { name: /map/i })).not.toBeInTheDocument();
});

// The retired `tr-sidebar-view` pref is cleared once on mount so stale "overview" values from a
// previous build don't linger in localStorage (#424 Phase 1).
test("a stale tr-sidebar-view pref is cleared on mount (#424)", async () => {
  localStorage.setItem("tr-sidebar-view", "overview");
  render(<App />);
  await screen.findByRole("link", { name: /new session/i });
  await waitFor(() =>
    expect(localStorage.getItem("tr-sidebar-view")).toBeNull(),
  );
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
    expect(api.setPrefs).toHaveBeenCalledWith({
      session_list_order: "created_at",
    }),
  );
  // The save refreshes the shared config…
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(2));
  // …whose new order triggers exactly one page-0 refetch, re-sorting the list in place.
  await waitFor(() =>
    expect(vi.mocked(api.sessions).mock.calls.length).toBe(fetches + 1),
  );
  expect(created).toHaveAttribute("aria-checked", "true"); // reconciled, not reverted
});

test("sidebar: a failed order save snaps the toggle back to the server truth (#548)", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("boom"));
  render(<App />);
  const created = await screen.findByRole("radio", { name: "Created" });
  await userEvent.click(created);
  await waitFor(() => expect(created).toHaveAttribute("aria-checked", "false"));
  expect(screen.getByRole("radio", { name: "Recent" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(api.config).toHaveBeenCalledTimes(1); // no config refresh on a failed save
});

// #283: on mobile, same-route nav targets (New session while already on that route) don't change
// location.pathname, so the route-change effect never closes the drawer. The shared
// closeMobileDrawer handler wired onto those links must close it in one tap. (Settings used to be
// a case here; since #1085 it is in the operator menu, which is outside the drawer.)
test.each([
  ["New session", /new session/i],
])(
  "mobile: tapping same-route %s closes the open drawer in one tap (#283)",
  async (_label, name) => {
    mockMobileViewport();
    const { container } = render(<App />);
    const app = container.querySelector(".app") as HTMLElement;

    // Open the off-canvas drawer (mobile toggle drives navOpen, not the desktop collapse flag).
    await userEvent.click(
      await screen.findByRole("button", { name: "Open session list" }),
    );
    expect(app).toHaveClass("navOpen");

    // Tap the same-route link — there can be two copies (topbar + in-drawer); either carries the
    // close handler, so the first is enough.
    const links = await screen.findAllByRole("link", { name });
    await userEvent.click(links[0]);

    await waitFor(() => expect(app).not.toHaveClass("navOpen"));
    // The desktop collapse flag must stay untouched (the two surfaces are independent, #128).
    expect(app).not.toHaveClass("collapsed");
  },
);

// --- Settings has no session sidebar (#1129) ----------------------------------------------------

// The shell carries the session sidebar on every work route; Settings is the utility surface
// that drops it (#1129) — and it drops it HIDDEN, never unmounted: the aside (and its
// SessionList) stay in the tree because the sidebar's rows, cursor and poll must survive a
// Settings visit exactly as they survive a collapsed one (#1007). jsdom applies no CSS, so what
// is asserted here is the derivation — the class, the standing-down toggle, the still-mounted
// list — while `e2e/settings-no-sidebar.spec.ts` pins the visible result in a real browser.
test("Settings renders without the session sidebar: noSidebar class, no toggle, list still mounted (#1129)", async () => {
  window.history.replaceState(null, "", "/settings");
  const { container } = render(<App />);
  await screen.findByRole("radiogroup", { name: "Order" }); // the shell's sidebar is mounted
  const app = container.querySelector(".app") as HTMLElement;
  expect(app).toHaveClass("noSidebar");
  // The operator's own collapse pref must be untouched by the route change.
  expect(app).not.toHaveClass("collapsed");
  // No control for a surface that does not exist there.
  expect(container.querySelector("header .navToggle")).toBeNull();
  // Hidden, not unmounted.
  expect(container.querySelector("aside.sidebar")).not.toBeNull();
});

test("a work route keeps the sidebar and its toggle — the removal is Settings-specific (#1129)", async () => {
  window.history.replaceState(null, "", "/");
  const { container } = render(<App />);
  expect(
    await screen.findByRole("button", { name: "Collapse session list" }),
  ).toBeInTheDocument();
  const app = container.querySelector(".app") as HTMLElement;
  expect(app).not.toHaveClass("noSidebar");
  expect(container.querySelector("aside.sidebar")).not.toBeNull();
});

test("the New project wizard renders without the session sidebar too (#1187)", async () => {
  window.history.replaceState(null, "", "/projects/new");
  const { container } = render(<App />);
  await screen.findByRole("heading", { name: "Name the project" });
  const app = container.querySelector(".app") as HTMLElement;
  expect(app).toHaveClass("noSidebar");
  expect(container.querySelector("header .navToggle")).toBeNull();
  // Hidden, not unmounted (#1007's continuity contract).
  expect(container.querySelector("aside.sidebar")).not.toBeNull();
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
  expect(
    screen.queryByRole("button", { name: /tap to reload/i }),
  ).not.toBeInTheDocument();
});

test("a swapped-in SW shell surfaces the tap-to-reload chip; tap applies via the SW path (#661)", async () => {
  swSwapped = true;
  render(<App />);
  const chip = await screen.findByRole("button", {
    name: /ready — tap to reload/i,
  });
  await userEvent.click(chip);
  expect(vi.mocked(applySWUpdate)).toHaveBeenCalledTimes(1); // SW-aware reload, never bare
});
