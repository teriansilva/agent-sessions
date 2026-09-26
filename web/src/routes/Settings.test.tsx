import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { OverviewPrefsProvider } from "../app/OverviewPrefsContext";
import { api } from "../lib/api";
import type { AgentUsageResponse, AppConfig, EngineInfo } from "../types/api";
import fixture from "../test/roster.fixture.json";
import type { ThemeId } from "../theme/themes";
import { ThemeCtx } from "../theme/themeStore";
import { AccentCtx } from "../theme/accentStore";
import { Settings } from "./Settings";
import { SETTINGS_SECTIONS } from "./settingsTabs";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      version: vi.fn(),
      setTheme: vi.fn(),
      setAccent: vi.fn(),
      engines: vi.fn(),
      agentUsage: vi.fn(),
      agentUsageRefresh: vi.fn(),
      setAgentBudgets: vi.fn(),
      system: vi.fn(),
      updateCheck: vi.fn(),
      updateApply: vi.fn(),
      // #1085: the Updates card reads the installer's progress record.
      updateProgress: vi.fn().mockResolvedValue({ state: "idle", steps: 7 }),
      updateSettings: vi.fn(),
      setUpdateSettings: vi.fn(),
      config: vi.fn(),
      enroll2fa: vi.fn(),
      confirm2fa: vi.fn(),
      disable2fa: vi.fn(),
      regenerate2fa: vi.fn(),
      logout: vi.fn(),
      archiveOlder: vi.fn(),
      setPrefs: vi.fn(),
      folders: vi.fn(),
      scrollbackInfo: vi.fn(),
      clearScrollback: vi.fn(),
      // #993: the Maintenance page's Prune + Archive old missions cards.
      compactInfo: vi.fn(),
      compact: vi.fn(),
      pruneInfo: vi.fn(),
      prune: vi.fn(),
      archiveOldMissionsInfo: vi.fn(),
      archiveOldMissions: vi.fn(),
      sessions: vi.fn(),
      aiReviewModels: vi.fn(),
      reviewExclude: vi.fn(),
      projectEntities: vi.fn(),
      createProject: vi.fn(),
      patchProject: vi.fn(),
      deleteProject: vi.fn(),
      archiveProject: vi.fn(),
      unarchiveProject: vi.fn(),
      // #465: the Folder discovery card opens the FolderPickerModal (api.fsDirs/fsMkdir).
      fsDirs: vi.fn(),
      fsMkdir: vi.fn(),
      // #441: the AI Review tab now also mounts the AI-activity panel + Pulse section.
      aiActivity: vi.fn().mockResolvedValue({ running: [], last: {} }),
      pulseScan: vi.fn(),
      // #956: the Prompts page renders the catalog (a deep link lands there).
      prompts: vi.fn().mockResolvedValue({ prompts: [] }),
      // #956: the Endpoint & model page checks a draft through this route.
      testAiEndpoint: vi.fn(),
      // #1009: the Usage analytics page saves consent through this route.
      setAnalyticsConsent: vi.fn(),
    },
  };
});

/** Surfaces the live router location (path + hash) so tests can assert the /settings/:section
 *  URL contract, including where a `#prompt-<id>` deep link lands. */
function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {location.pathname}
      {location.hash}
    </div>
  );
}

/** Mounts Settings under the real route shapes (#357): bare /settings and /settings/:tab —
 *  both render the component; the bare/unknown forms replace-redirect to the first tab. */
function renderSettings(
  theme: ThemeId = "dark",
  accent = "#ffb000",
  initialPath = "/settings",
  authMode: "single-user" | "none" = "single-user",
  configOver: Partial<AppConfig> = {},
) {
  const setTheme = vi.fn();
  const setAccent = vi.fn();
  const publishRef: { current: (c: AppConfig) => void } = { current: () => {} };
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      {/* #682: the Security tab is now config-driven (useConfig gates the login-off vs 2FA/Account
          cards), so the harness must provide a resolved config — single-user here, matching the
          api.config mock — or the panel renders empty. */}
      <ConfigHost
        publishRef={publishRef}
        initial={
          {
            csrf: "t",
            new_session_engines: [],
            terminal_backend: "ws",
            auth_mode: authMode,
            two_factor_enabled: false,
            ...configOver,
          } as AppConfig
        }
      >
        <ThemeCtx.Provider value={{ theme, setTheme }}>
          <AccentCtx.Provider value={{ accent, setAccent }}>
            <OverviewPrefsProvider>
              <Routes>
                <Route path="/settings" element={<Settings />} />
                <Route path="/settings/:tab" element={<Settings />} />
                <Route path="/settings/agents/:agent" element={<Settings />} />
              </Routes>
              <LocationProbe />
            </OverviewPrefsProvider>
          </AccentCtx.Provider>
        </ThemeCtx.Provider>
      </ConfigHost>
    </MemoryRouter>,
  );
  return {
    setTheme,
    setAccent,
    publishConfig: (c: AppConfig) => publishRef.current(c),
  };
}

/** Holds the config in state so a test can publish a newer one — what ConfigContext does when a
 *  refresh lands (#1009: a setup replay saving over a mounted Settings page). */
function ConfigHost({
  initial,
  publishRef,
  children,
}: {
  initial: AppConfig;
  publishRef: { current: (c: AppConfig) => void };
  children: React.ReactNode;
}) {
  const [config, setConfig] = useState(initial);
  useEffect(() => {
    publishRef.current = setConfig;
  }, [publishRef]);
  return <ConfigCtx.Provider value={config}>{children}</ConfigCtx.Provider>;
}

/** The About tab is the only one that shows the version — on other tabs, flush the pending
 *  mount-time fetches (version, config, …) inside act() so their state updates don't land
 *  after the test ends. */
const flushFetches = () =>
  act(async () => {
    await Promise.resolve();
  });

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  vi.mocked(api.compactInfo).mockResolvedValue({
    compact: {
      available: false, db_bytes: null, wal_bytes: null, reclaimable_bytes: null,
      holders: null, disk: null,
      blockers: [{ code: "missing", detail: "No OpenCode database exists on this host." }],
    },
    job: null, runner: null,
  });
  vi.mocked(api.version).mockResolvedValue({ version: "1.2.3" });
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "single-user",
    two_factor_enabled: false,
  });
  // The manifest-generated roster, with claude at a real path and codex not installed.
  vi.mocked(api.engines).mockResolvedValue({
    engines: (fixture.engines as EngineInfo[]).map((e) =>
      e.id === "claude"
        ? { ...e, bin: "/usr/local/bin/claude" }
        : e.id === "codex"
          ? { ...e, present: false, bin: null }
          : e,
    ),
    problems: [],
  });
  vi.mocked(api.agentUsage).mockResolvedValue({
    budgets: { threshold_pct: 90, notify: true, engines: {} },
    agents: [
      {
        engine: "claude",
        source: "plan",
        at: Date.now() / 1000,
        checked_at: Date.now() / 1000,
        stale: false,
        limit_tokens: 0,
        manual_used: 0,
        used_pct: 93,
        plan: "max",
        windows: [
          { label: "session", used_pct: 4, resets_at: null },
          { label: "week (all models)", used_pct: 93, resets_at: null },
        ],
      },
      {
        engine: "codex",
        source: "none",
        at: 0,
        checked_at: null,
        stale: false,
        limit_tokens: 0,
        manual_used: 0,
        used_pct: null,
      },
    ],
  });
  vi.mocked(api.system).mockResolvedValue({
    os: "Linux 6.8.0",
    platform: "Linux-6.8.0-x86_64",
    arch: "x86_64",
    python: "3.12.1",
    version: "9.9.9",
    hostname: "host",
    cpus: 8,
    load: { "1": 0.5, "5": 0.4, "15": 0.3 },
    mem_total: 16 * 1024 ** 3,
    mem_available: 8 * 1024 ** 3,
    disk_total: 500 * 1024 ** 3,
    disk_free: 200 * 1024 ** 3,
    uptime_seconds: 90000,
  });
  // Updates card (#538): persisted settings load on mount (cheap GET, no remote hit).
  vi.mocked(api.updateSettings).mockResolvedValue({
    auto_update: false,
    channel: "stable",
    last_auto: null,
  });
  vi.mocked(api.setUpdateSettings).mockImplementation((body) =>
    Promise.resolve({
      auto_update: body.auto_update ?? false,
      channel: body.channel ?? "stable",
      last_auto: null,
    }),
  );
  vi.mocked(api.logout).mockResolvedValue(undefined);
  vi.mocked(api.archiveOlder).mockResolvedValue({ archived: 0, skipped: 0 });
  vi.mocked(api.setPrefs).mockResolvedValue({});
  vi.mocked(api.folders).mockResolvedValue({ folders: [] });
  vi.mocked(api.scrollbackInfo).mockResolvedValue({ bytes: 0, files: 0 });
  vi.mocked(api.clearScrollback).mockResolvedValue({
    scope: "all",
    removed: 0,
    bytes_freed: 0,
  });
  // Maintenance (#993): nothing to prune and no eligible missions by default.
  vi.mocked(api.pruneInfo).mockResolvedValue({
    categories: {
      stale_sockets: { items: 0, bytes: 0 },
      archived_scrollback: { items: 0, bytes: 0 },
    },
    runner: null,
  });
  vi.mocked(api.archiveOldMissionsInfo).mockResolvedValue({
    eligible: 0,
    sessions: 0,
    live_sessions: 0,
    unresolved: [],
    runner: null,
  });
  // AI Review tab (#356): no sessions excluded, model listing unsupported by default.
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [],
    next_offset: null,
    total: 0,
    facets: { projects: [], engines: [] },
  });
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: [] });
  vi.mocked(api.reviewExclude).mockResolvedValue({
    id: "x",
    review_excluded: false,
  });
  // Projects manager (#361 Phase 3): no entities by default.
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [] });
  // Folder picker (#465 / #448): a simple home listing so the discovery picker can open + select.
  vi.mocked(api.fsDirs).mockResolvedValue({
    path: "/home/u",
    home: "/home/u",
    dirs: [{ name: "code", path: "/home/u/code" }],
  });
  vi.mocked(api.fsMkdir).mockResolvedValue({ path: "/home/u/new" });
});

// ---- Settings navigation: sections, routing + deep links (#956) ----

test("bare /settings redirects to the first section on desktop (canonical /settings/:section)", async () => {
  renderSettings("dark", "#ffb000", "/settings");
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/settings/appearance",
  );
  expect(screen.getByRole("link", { name: "Appearance" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await flushFetches();
});

test("an unknown section falls back to the first section (no 404)", async () => {
  renderSettings("dark", "#ffb000", "/settings/launch-codes");
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/settings/appearance",
  );
  expect(
    screen.getByRole("heading", { name: "Appearance" }),
  ).toBeInTheDocument();
  await flushFetches();
});

test("the sidebar lists every section, grouped, in registry order — one current page", async () => {
  renderSettings("dark", "#ffb000", "/settings/projects");
  const nav = screen.getByRole("navigation", { name: "Settings" });
  const links = within(nav).getAllByRole("link");
  expect(links.map((l) => sectionLabel(l))).toEqual(
    SETTINGS_SECTIONS.map((s) => s.label),
  );
  for (const group of ["General", "AI", "System", "About"]) {
    expect(within(nav).getByRole("list", { name: group })).toBeInTheDocument();
  }
  expect(
    links
      .filter((l) => l.getAttribute("aria-current") === "page")
      .map((l) => sectionLabel(l)),
  ).toEqual(["Projects"]);
  await flushFetches();
});

test("clicking a section navigates to its URL and swaps the page", async () => {
  renderSettings();
  await userEvent.click(screen.getByRole("link", { name: "Maintenance" }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/settings/maintenance",
  );
  expect(screen.getByRole("link", { name: "Maintenance" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(
    await screen.findByRole("heading", { name: "Archive old sessions" }),
  ).toBeInTheDocument();
  // The previous page's content is gone.
  expect(
    screen.queryByRole("heading", { name: "Appearance" }),
  ).not.toBeInTheDocument();
});

test.each([
  ["/settings/ai-review", "/settings/ai-endpoint"],
  [
    "/settings/ai-review#prompt-chat_instruct",
    "/settings/ai-prompts#prompt-chat_instruct",
  ],
])("the pre-#956 AI tab link %s lands on %s", async (from, to) => {
  renderSettings("dark", "#ffb000", from);
  await waitFor(() =>
    expect(screen.getByTestId("location")).toHaveTextContent(to),
  );
  await flushFetches();
});

test("the page names its group and section in a crumb, not a second heading", async () => {
  renderSettings("dark", "#ffb000", "/settings/ai-mission-control");
  expect(screen.getByText(/Settings \/\/ AI \/\//)).toHaveTextContent(
    "Mission control",
  );
  await flushFetches();
});

// Every existing settings control still has a home (#357 zero-behavioural-change guarantee, kept
// through the #956 split): each section renders its cards.
test.each([
  ["appearance", ["Appearance"]],
  ["session-defaults", ["Session defaults"]],
  ["projects", ["Projects", "Session overview"]],
  ["ai-endpoint", ["Connection", "Model"]],
  ["ai-session-review", ["Session review"]],
  ["ai-auto-sort", ["Auto-sort projects"]],
  ["ai-mission-control", ["Orchestrator", "Session scan", "Forge connection"]],
  ["ai-playbooks", ["Mission checklists"]],
  ["ai-prompts", ["Prompts"]],
  ["ai-activity", ["AI activity"]],
  ["agents", ["Agents"]],
  ["agents-defaults", ["Defaults"]],
  ["security", ["Two-factor authentication", "Account"]],
  ["updates", ["Updates"]],
  ["analytics", ["Usage analytics"]],
  ["system", ["Host"]],
  [
    "maintenance",
    ["Archive old sessions", "Archive old missions", "Scrollback cache", "Prune"],
  ],
  ["about", ["Support", "About"]],
])("section %s renders its cards: %s", async (section, headings) => {
  renderSettings("dark", "#ffb000", `/settings/${section}`);
  for (const h of headings) {
    expect(await screen.findByRole("heading", { name: h })).toBeInTheDocument();
  }
  await flushFetches();
});

test("the registry and the pages agree: every section id renders something", () => {
  // A registry entry with no body in Settings.tsx would be a nav link to a blank page.
  expect(SETTINGS_SECTIONS).toHaveLength(18);
});

// ---- Usage analytics (#1009) ----------------------------------------------------------------

const UNDECIDED = { enabled: false, decided: false, available: true };
const ANALYTICS_ON = { enabled: true, decided: true, available: true };

function analyticsBox() {
  return screen.getByRole("checkbox", { name: "Share usage analytics" });
}

test("usage analytics: an undecided install shows off, and ticking saves true", async () => {
  vi.mocked(api.setAnalyticsConsent).mockResolvedValue({
    analytics: ANALYTICS_ON,
  });
  renderSettings("dark", "#ffb000", "/settings/analytics", "single-user", {
    analytics: UNDECIDED,
  });
  expect(analyticsBox()).not.toBeChecked();
  await userEvent.click(analyticsBox());
  expect(api.setAnalyticsConsent).toHaveBeenCalledWith(true);
  await waitFor(() => expect(analyticsBox()).toBeChecked());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await flushFetches();
});

test("usage analytics: a failed save shows the server's state, not the click", async () => {
  vi.mocked(api.setAnalyticsConsent).mockRejectedValue(new Error("offline"));
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    analytics: ANALYTICS_ON,
  });
  renderSettings("dark", "#ffb000", "/settings/analytics", "single-user", {
    analytics: ANALYTICS_ON,
  });
  await userEvent.click(analyticsBox()); // untick
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "your previous setting (on) is still in effect",
  );
  expect(analyticsBox()).toBeChecked();
  await flushFetches();
});

test("usage analytics: a save whose response was lost but which landed is a success", async () => {
  vi.mocked(api.setAnalyticsConsent).mockRejectedValue(new Error("timeout"));
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    analytics: { enabled: false, decided: true, available: true },
  });
  renderSettings("dark", "#ffb000", "/settings/analytics", "single-user", {
    analytics: ANALYTICS_ON,
  });
  await userEvent.click(analyticsBox());
  await waitFor(() => expect(analyticsBox()).not.toBeChecked());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await flushFetches();
});

test("usage analytics: a newer config replaces what the last save showed (setup replayed over the page)", async () => {
  vi.mocked(api.setAnalyticsConsent).mockResolvedValue({
    analytics: { enabled: false, decided: true, available: true },
  });
  const { publishConfig } = renderSettings(
    "dark",
    "#ffb000",
    "/settings/analytics",
    "single-user",
    { analytics: ANALYTICS_ON },
  );
  await userEvent.click(analyticsBox()); // off, here
  await waitFor(() => expect(analyticsBox()).not.toBeChecked());
  // …then the wizard, replayed from Help over this mounted page, turns it back on and refreshes.
  act(() =>
    publishConfig({
      csrf: "t",
      new_session_engines: [],
      terminal_backend: "ws",
      auth_mode: "single-user",
      analytics: ANALYTICS_ON,
    } as AppConfig),
  );
  await waitFor(() => expect(analyticsBox()).toBeChecked());
  await flushFetches();
});

test("usage analytics: a stale config landing during the save does not roll the result back", async () => {
  let resolveSave: (v: { analytics: typeof ANALYTICS_ON }) => void = () => {};
  vi.mocked(api.setAnalyticsConsent).mockReturnValue(
    new Promise((res) => {
      resolveSave = res;
    }),
  );
  const { publishConfig } = renderSettings(
    "dark",
    "#ffb000",
    "/settings/analytics",
    "single-user",
    { analytics: ANALYTICS_ON },
  );
  await userEvent.click(analyticsBox()); // off — pending
  expect(analyticsBox()).toBeDisabled();
  // A read issued before the save resolves meanwhile, still carrying "on".
  act(() =>
    publishConfig({
      csrf: "t",
      new_session_engines: [],
      terminal_backend: "ws",
      auth_mode: "single-user",
      analytics: ANALYTICS_ON,
    } as AppConfig),
  );
  await act(async () => {
    resolveSave({ analytics: { enabled: false, decided: true, available: true } });
  });
  await waitFor(() => expect(analyticsBox()).not.toBeChecked());
  await flushFetches();
});

test("usage analytics: the server's kill switch disables the toggle and says why", async () => {
  renderSettings("dark", "#ffb000", "/settings/analytics", "single-user", {
    analytics: { enabled: true, decided: true, available: false },
  });
  expect(analyticsBox()).toBeDisabled();
  expect(analyticsBox()).not.toBeChecked();
  expect(
    screen.getByText(/Turned off for this server by AGENT_SESSIONS_ANALYTICS=0/),
  ).toBeInTheDocument();
  await flushFetches();
});

test("the Endpoint & model page renders the endpoint fields", async () => {
  renderSettings("dark", "#ffb000", "/settings/ai-endpoint");
  expect(
    await screen.findByRole("heading", { name: /Connection/ }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText(/API key/i)).toBeInTheDocument();
  // Session review is its own page now.
  expect(
    screen.queryByRole("heading", { name: "Session review" }),
  ).not.toBeInTheDocument();
  await flushFetches();
});

/** A nav row's section label. The Endpoint & model row also carries its status LED (and, on a
 *  phone, the active model), so the label is read from its own marked span (#956). */
function sectionLabel(link: HTMLElement): string | null | undefined {
  return link.querySelector("[data-section-label]")?.textContent;
}

/** A phone viewport for `useIsMobile` (jsdom has no matchMedia of its own). */
function asPhone(): () => void {
  const original = window.matchMedia;
  window.matchMedia = vi.fn().mockReturnValue({
    matches: true,
    media: "(max-width: 800px)",
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }) as unknown as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

test("phone: bare /settings is the grouped index, and a section opens full-width with a back link (#956)", async () => {
  const restore = asPhone();
  try {
    renderSettings("dark", "#ffb000", "/settings");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/settings$/);
    const nav = screen.getByRole("navigation", { name: "Settings" });
    expect(within(nav).getAllByRole("link").map((l) => sectionLabel(l))).toEqual(
      SETTINGS_SECTIONS.map((s) => s.label),
    );
    expect(
      screen.getByRole("link", { name: "Back to sessions" }),
    ).toBeInTheDocument();

    await userEvent.click(within(nav).getByRole("link", { name: "Updates" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/settings/updates",
    );
    expect(
      await screen.findByRole("heading", { name: "Updates" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to settings" }),
    ).toHaveAttribute("href", "/settings");
    // No sidebar on a phone: the index is the navigation.
    expect(
      screen.queryByRole("navigation", { name: "Settings" }),
    ).not.toBeInTheDocument();
  } finally {
    restore();
  }
});

test("phone: an unknown section lands on the index, not the first section", async () => {
  const restore = asPhone();
  try {
    renderSettings("dark", "#ffb000", "/settings/launch-codes");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(/^\/settings$/),
    );
    expect(
      screen.getByRole("navigation", { name: "Settings" }),
    ).toBeInTheDocument();
  } finally {
    restore();
  }
});

// ---- Appearance tab ----

test("renders the themes and the theme radios", async () => {
  renderSettings();
  expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  for (const label of ["Dark", "Light"]) {
    expect(
      screen.getByRole("radio", { name: new RegExp(label) }),
    ).toBeInTheDocument();
  }
  await flushFetches();
});

test("the active theme is marked aria-checked", async () => {
  renderSettings("light");
  expect(screen.getByRole("radio", { name: /Light/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByRole("radio", { name: /Dark/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await flushFetches();
});

test("picking a theme calls setTheme with its id", async () => {
  const { setTheme } = renderSettings("light");
  await userEvent.click(screen.getByRole("radio", { name: /Dark/ }));
  expect(setTheme).toHaveBeenCalledWith("dark");
  await flushFetches();
});

test("picking an accent preset calls setAccent with its hex (#211 Phase 2)", async () => {
  const { setAccent } = renderSettings("dark");
  await userEvent.click(screen.getByRole("radio", { name: "Signal Red" }));
  expect(setAccent).toHaveBeenCalledWith("#c02020");
  await flushFetches();
});

test("the active accent preset is marked aria-checked", async () => {
  renderSettings("dark", "#c02020");
  expect(screen.getByRole("radio", { name: "Signal Red" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByRole("radio", { name: "Amber" })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await flushFetches();
});

test("committing a custom hex (Enter) calls setAccent normalized", async () => {
  const { setAccent } = renderSettings("dark");
  const field = screen.getByLabelText("Accent hex value");
  await userEvent.clear(field);
  await userEvent.type(field, "#3FBF6F{Enter}");
  expect(setAccent).toHaveBeenCalledWith("#3fbf6f");
  await flushFetches();
});

test("an invalid custom hex is rejected (no setAccent) and the field resets", async () => {
  const { setAccent } = renderSettings("dark", "#ffb000");
  const field = screen.getByLabelText("Accent hex value");
  await userEvent.clear(field);
  await userEvent.type(field, "zzz{Enter}");
  expect(setAccent).not.toHaveBeenCalled();
  expect(field).toHaveValue("#ffb000"); // reset to the active accent
  await flushFetches();
});

// ---- System tab ----

test("the roster renders a card per engine: its state, its path and its capabilities (#1128)", async () => {
  renderSettings("dark", "#ffb000", "/settings/agents");
  expect(screen.getByRole("heading", { name: "Agents" })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByText("/usr/local/bin/claude")).toBeInTheDocument(),
  );
  const claude = screen.getByRole("heading", { name: "Claude Code" })
    .closest("li")!;
  expect(within(claude).getByText("Present")).toBeInTheDocument();
  expect(within(claude).getByText(/Runtime \/\/ terminal/i)).toBeInTheDocument();
  const codex = screen.getByRole("heading", { name: "Codex" }).closest("li")!;
  // An absent engine says so, and shows no path it does not have.
  expect(within(codex).getByText("Absent")).toBeInTheDocument();
  expect(within(codex).getByText(/not installed on this host/i)).toBeInTheDocument();
  // Undeclared capabilities are shown OFF, not hidden: gemini is no handoff target.
  const gemini = screen.getByRole("heading", { name: "Gemini CLI" })
    .closest("li")!;
  const caps = within(gemini).getByRole("list", { name: /capabilities/ });
  expect(within(caps).getByText(/handoff target/)).toHaveTextContent("(off)");
  expect(within(caps).getByText(/^resume$/)).toBeInTheDocument();
  // Each card links to the agent's own page.
  expect(
    within(claude).getByRole("link", { name: /details for claude code/i }),
  ).toHaveAttribute("href", "/settings/agents/claude");
});

test("shows what an agent has spent on the row that already names it (#839)", async () => {
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());
  // The window nearest its limit, not the first one the agent listed.
  expect(screen.getByText(/week \(all models\)/)).toBeInTheDocument();
  expect(screen.queryByText("4%")).not.toBeInTheDocument();
  const meter = screen.getByRole("meter", { name: /claude usage/i });
  expect(meter).toHaveAttribute("aria-valuenow", "93");
  // The SOURCE is on the row itself — scoped, because the section hint also says "plan".
  // A plan percentage and a counter the operator typed are different claims, and a panel
  // that renders them identically is lying by omission.
  expect(within(meter.parentElement!).getByText("plan")).toBeInTheDocument();
});

test("an agent that reports nothing shows no percentage, not a zero (#839)", async () => {
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());
  // codex reports nothing and has no limit set: an em dash, never "0%" — which would read
  // as "this agent has used nothing".
  const meter = screen.getByRole("meter", { name: /codex usage/i });
  expect(meter).not.toHaveAttribute("aria-valuenow");
  // Scoped to the meter row: the absent-binary placeholder is also an em dash.
  expect(within(meter.parentElement!).getByText("—")).toBeInTheDocument();
  expect(screen.getByText(/reports no usage/)).toBeInTheDocument();
});

test("a plan agent is offered no limit box to set (#839)", async () => {
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());
  // claude reports a real quota against a real plan; a "limit" input there would invite the
  // operator to configure a number the agent already knows better.
  expect(
    screen.queryByRole("spinbutton", { name: /claude token limit/i }),
  ).not.toBeInTheDocument();
  // codex has no engine-reported figure, so its counter IS the operator's to set.
  expect(
    screen.getByRole("spinbutton", { name: /codex token limit/i }),
  ).toBeInTheDocument();
});

test("saving a limit sends only that agent's field (#839)", async () => {
  vi.mocked(api.setAgentBudgets).mockResolvedValue({
    budgets: {
      threshold_pct: 90,
      notify: true,
      engines: { codex: { limit_tokens: 5000 } },
    },
    agents: [],
  });
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());
  const input = screen.getByRole("spinbutton", { name: /codex token limit/i });
  fireEvent.change(input, { target: { value: "5000" } });
  fireEvent.blur(input);
  await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalled());
  // Per-engine, per-field: a panel saving one agent's limit must not carry another's.
  expect(vi.mocked(api.setAgentBudgets).mock.calls[0][0]).toEqual({
    engines: { codex: { limit_tokens: 5000 } },
  });
});

test("saves are serialized, so the last response is the newest state (#839)", async () => {
  // Every save returns the WHOLE snapshot, so two in flight are last-response-wins. A client
  // sequence counter cannot fix that: partial PATCHes merge under the server's lock, so the
  // request that STARTED first can SETTLE last and carry the newest authoritative document —
  // discarding it as "superseded" would throw away the only correct snapshot.
  //
  // Serializing removes the question. This asserts the mechanism: the second PATCH is not even
  // issued until the first has settled.
  const deferred: ((v: AgentUsageResponse) => void)[] = [];
  vi.mocked(api.setAgentBudgets).mockImplementation(
    () => new Promise<AgentUsageResponse>((res) => deferred.push(res)),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const snapshot = (notifyValue: boolean): AgentUsageResponse => ({
    budgets: { threshold_pct: 90, notify: notifyValue, engines: {} },
    agents: [],
  });

  fireEvent.click(screen.getByRole("checkbox", { name: /notify me/i })); // save #1
  const limit = screen.getByRole("spinbutton", { name: /codex token limit/i });
  fireEvent.change(limit, { target: { value: "7000" } });
  fireEvent.blur(limit); // save #2
  await waitFor(() => expect(deferred.length).toBe(1));
  // Still one: the second is queued behind it, not racing it.
  expect(deferred.length).toBe(1);

  await act(async () => {
    deferred[0](snapshot(true));
  });
  await waitFor(() => expect(deferred.length).toBe(2));
  await act(async () => {
    deferred[1](snapshot(false));
  });

  // The last response settled last, so the panel shows it.
  const box = screen.getByRole("checkbox", {
    name: /notify me/i,
  }) as HTMLInputElement;
  expect(box.checked).toBe(false);
});

test("two quick notify toggles send off then on, not off twice (#839)", async () => {
  // The checkbox was controlled purely by the server snapshot, so while the first PATCH was in
  // flight it still rendered the OLD value — and the second click computed `!old` again,
  // enqueuing the same write twice. The operator's second toggle was silently lost.
  const deferred: ((v: AgentUsageResponse) => void)[] = [];
  vi.mocked(api.setAgentBudgets).mockImplementation(
    () => new Promise<AgentUsageResponse>((res) => deferred.push(res)),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const box = screen.getByRole("checkbox", {
    name: /notify me/i,
  }) as HTMLInputElement;
  expect(box.checked).toBe(true);

  fireEvent.click(box); // → off
  await waitFor(() => expect(deferred.length).toBe(1));
  expect(box.checked).toBe(false); // the pending intent shows immediately

  fireEvent.click(box); // → back on, while the first is still in flight
  expect(box.checked).toBe(true);

  await act(async () => {
    deferred[0]({
      budgets: { threshold_pct: 90, notify: false, engines: {} },
      agents: [],
    });
  });
  await waitFor(() => expect(deferred.length).toBe(2));

  expect(vi.mocked(api.setAgentBudgets).mock.calls[0][0]).toEqual({
    notify: false,
  });
  expect(vi.mocked(api.setAgentBudgets).mock.calls[1][0]).toEqual({
    notify: true,
  });
});

test.each([
  ["alert threshold", (v: string) => ({ threshold_pct: Number(v) })],
] as const)(
  "a change-away-then-back on %s still reaches the server (#839)",
  async (label, expected) => {
    // Threshold is 90. Type 80, blur (PATCH queued). Type 90, blur again — compared against the
    // rendered snapshot (still 90) that second blur looks like a no-op, so nothing compensates
    // and the queued 80 commits. The comparison has to be against the latest INTENT.
    const deferred: ((v: AgentUsageResponse) => void)[] = [];
    vi.mocked(api.setAgentBudgets).mockImplementation(
      () => new Promise<AgentUsageResponse>((res) => deferred.push(res)),
    );
    renderSettings("dark", "#ffb000", "/settings/agents");
    await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

    const input = screen.getByRole("spinbutton", {
      name: new RegExp(label, "i"),
    }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "80" } });
    fireEvent.blur(input);
    await waitFor(() => expect(deferred.length).toBe(1));

    fireEvent.change(input, { target: { value: "90" } });
    fireEvent.blur(input);

    await act(async () => {
      deferred[0]({
        budgets: { threshold_pct: 80, notify: true, engines: {} },
        agents: [],
      });
    });

    await waitFor(() => expect(deferred.length).toBe(2));
    expect(vi.mocked(api.setAgentBudgets).mock.calls[0][0]).toEqual(
      expected("80"),
    );
    expect(vi.mocked(api.setAgentBudgets).mock.calls[1][0]).toEqual(
      expected("90"),
    );
  },
);

test("a change-away-then-back on a per-agent limit still reaches the server (#839)", async () => {
  const deferred: ((v: AgentUsageResponse) => void)[] = [];
  vi.mocked(api.setAgentBudgets).mockImplementation(
    () => new Promise<AgentUsageResponse>((res) => deferred.push(res)),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const limit = screen.getByRole("spinbutton", {
    name: /codex token limit/i,
  }) as HTMLInputElement;
  // Stored is 0 (unset). Ask for 5000, then back to 0 while the first is in flight.
  fireEvent.change(limit, { target: { value: "5000" } });
  fireEvent.blur(limit);
  await waitFor(() => expect(deferred.length).toBe(1));
  fireEvent.change(limit, { target: { value: "0" } });
  fireEvent.blur(limit);

  await act(async () => {
    deferred[0]({
      budgets: {
        threshold_pct: 90,
        notify: true,
        engines: { codex: { limit_tokens: 5000 } },
      },
      agents: [],
    });
  });
  await waitFor(() => expect(deferred.length).toBe(2));
  expect(vi.mocked(api.setAgentBudgets).mock.calls[1][0]).toEqual({
    engines: { codex: { limit_tokens: 0 } },
  });
});

test("a rejected save can be retried with the same value (#839)", async () => {
  // The field snaps back to the stored number after a rejection, so the operator types the same
  // thing again — and it compared equal to the intent the FAILED request had left behind, so
  // nothing was sent. A transient 500 made that value permanently unsettable.
  vi.mocked(api.setAgentBudgets).mockRejectedValueOnce(
    new Error("boom, try again"),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const input = screen.getByRole("spinbutton", {
    name: /alert threshold/i,
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "80" } });
  fireEvent.blur(input);
  await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(input.value).toBe("90")); // snapped back to stored

  vi.mocked(api.setAgentBudgets).mockResolvedValue({
    budgets: { threshold_pct: 80, notify: true, engines: {} },
    agents: [],
  });
  fireEvent.change(input, { target: { value: "80" } });
  fireEvent.blur(input);
  await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.setAgentBudgets).mock.calls[1][0]).toEqual({
    threshold_pct: 80,
  });
});

test("a failed older save does not erase a newer queued intent (#839)", async () => {
  // The sequence that makes rolling back by field NAME lose data — and it is silent:
  //   stored 90 → submit 80 → queue 70 → the 80 request FAILS.
  // A name-keyed rollback deletes the *70* intent. The operator then types 90, which matches the
  // stored snapshot, so no compensating PATCH is queued — and the in-flight 70 commits over
  // their latest choice. Rolling back by VALUE keeps the failed request from reclaiming a field
  // a newer one already owns.
  const deferred: {
    resolve: (v: AgentUsageResponse) => void;
    reject: (e: Error) => void;
  }[] = [];
  vi.mocked(api.setAgentBudgets).mockImplementation(
    () =>
      new Promise<AgentUsageResponse>((resolve, reject) =>
        deferred.push({ resolve, reject }),
      ),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const input = screen.getByRole("spinbutton", {
    name: /alert threshold/i,
  }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "80" } });
  fireEvent.blur(input); // #1 → 80, in flight
  await waitFor(() => expect(deferred.length).toBe(1));
  fireEvent.change(input, { target: { value: "70" } });
  fireEvent.blur(input); // #2 → 70, queued behind it

  await act(async () => {
    deferred[0].reject(new Error("the 80 request failed"));
  });
  await waitFor(() => expect(deferred.length).toBe(2));

  // Back to 90 — the operator's latest choice, while 70 is still in flight.
  fireEvent.change(input, { target: { value: "90" } });
  fireEvent.blur(input);

  // Saves are serialized, so the compensating PATCH is QUEUED here rather than sent; letting
  // the 70 request settle releases it. The failure this guards is that it is never queued at
  // all, because the failed 80 deleted the 70 intent and 90 then looked like a no-op.
  await act(async () => {
    deferred[1].resolve({
      budgets: { threshold_pct: 70, notify: true, engines: {} },
      agents: [],
    });
  });

  await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalledTimes(3));
  expect(vi.mocked(api.setAgentBudgets).mock.calls[2][0]).toEqual({
    threshold_pct: 90,
  });
});

test.each([
  [
    "alert threshold",
    "90",
    ["80", "70", "80"],
    "90",
    (v: number) => ({ threshold_pct: v }),
  ],
  [
    "codex token limit",
    "",
    ["800", "700", "800"],
    "900",
    (v: number) => ({ engines: { codex: { limit_tokens: v } } }),
  ],
  [
    "codex tokens used",
    "",
    ["800", "700", "800"],
    "900",
    (v: number) => ({ engines: { codex: { manual_used: v } } }),
  ],
] as const)(
  "ABA: a failed request does not release a newer claim on %s (#839)",
  async (label, _stored, [a1, b1, a2], finalValue, expected) => {
    // Matching VALUES are not proof of ownership. Queue A → B → A; when the first A fails, a
    // value-keyed check sees the newest intent is also A, releases the third request's claim,
    // and the operator's next choice then compares equal to the stored snapshot — no
    // compensating PATCH, and the queued A becomes the durable value. Ownership is a revision
    // token, which has no ABA problem by construction.
    const deferred: {
      resolve: (v: AgentUsageResponse) => void;
      reject: (e: Error) => void;
    }[] = [];
    vi.mocked(api.setAgentBudgets).mockImplementation(
      () =>
        new Promise<AgentUsageResponse>((resolve, reject) =>
          deferred.push({ resolve, reject }),
        ),
    );
    renderSettings("dark", "#ffb000", "/settings/agents");
    await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

    const input = screen.getByRole("spinbutton", {
      name: new RegExp(label, "i"),
    }) as HTMLInputElement;
    for (const v of [a1, b1, a2]) {
      fireEvent.change(input, { target: { value: v } });
      fireEvent.blur(input);
    }
    await waitFor(() => expect(deferred.length).toBe(1));

    // The FIRST request fails while the third (same value) still owns the field.
    await act(async () => {
      deferred[0].reject(new Error("the first one failed"));
    });

    fireEvent.change(input, { target: { value: finalValue } });
    fireEvent.blur(input);

    // Saves are serialized: drain the queued ones so the compensating PATCH is sent.
    for (let i = 1; i < 4; i++) {
      await waitFor(() => expect(deferred.length).toBeGreaterThan(i - 1));
      if (!deferred[i]) break;
      await act(async () => {
        deferred[i].resolve({
          budgets: { threshold_pct: 90, notify: true, engines: {} },
          agents: [],
        });
      });
    }

    await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalledTimes(4));
    const calls = vi.mocked(api.setAgentBudgets).mock.calls;
    expect(calls[3][0]).toEqual(expected(Number(finalValue)));
  },
);

test.each([
  ["alert threshold", "80", "0", "90", (v: number) => ({ threshold_pct: v })],
  [
    "codex token limit",
    "800",
    "-5",
    "900",
    (v: number) => ({ engines: { codex: { limit_tokens: v } } }),
  ],
  [
    "codex tokens used",
    "800",
    "-5",
    "900",
    (v: number) => ({ engines: { codex: { manual_used: v } } }),
  ],
] as const)(
  "invalid input does not surrender an outstanding claim on %s (#839)",
  async (label, submitted, invalid, corrected, expected) => {
    // Typing something the server would refuse asks the server nothing — so it must not touch
    // the claim of a request that IS outstanding. Clearing it there loses the field: the box
    // snaps back to the stored value, and accepting that value then compares equal to the stored
    // snapshot, so nothing compensates and the in-flight request lands as the durable value.
    const deferred: {
      resolve: (v: AgentUsageResponse) => void;
      reject: (e: Error) => void;
    }[] = [];
    vi.mocked(api.setAgentBudgets).mockImplementation(
      () =>
        new Promise<AgentUsageResponse>((resolve, reject) =>
          deferred.push({ resolve, reject }),
        ),
    );
    renderSettings("dark", "#ffb000", "/settings/agents");
    await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

    const input = screen.getByRole("spinbutton", {
      name: new RegExp(label, "i"),
    }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: submitted } });
    fireEvent.blur(input); // in flight
    await waitFor(() => expect(deferred.length).toBe(1));

    // Invalid, so nothing is sent and the box snaps back.
    fireEvent.change(input, { target: { value: invalid } });
    fireEvent.blur(input);
    expect(api.setAgentBudgets).toHaveBeenCalledTimes(1);

    // The operator now settles on a real value while the first is still outstanding.
    fireEvent.change(input, { target: { value: corrected } });
    fireEvent.blur(input);

    await act(async () => {
      deferred[0].resolve({
        budgets: { threshold_pct: 90, notify: true, engines: {} },
        agents: [],
      });
    });

    await waitFor(() => expect(api.setAgentBudgets).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.setAgentBudgets).mock.calls[1][0]).toEqual(
      expected(Number(corrected)),
    );
  },
);

test("an older queued save does not clear a newer edit to the same field (#839)", async () => {
  // Saves queue, so an old response can arrive while the operator has already typed something
  // else into the same box. Clearing on the field NAME alone reverted the visible input to the
  // value the settled request had submitted, while the newer one was still pending.
  const deferred: ((v: AgentUsageResponse) => void)[] = [];
  vi.mocked(api.setAgentBudgets).mockImplementation(
    () => new Promise<AgentUsageResponse>((res) => deferred.push(res)),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const limit = screen.getByRole("spinbutton", {
    name: /codex token limit/i,
  }) as HTMLInputElement;
  fireEvent.change(limit, { target: { value: "7000" } });
  fireEvent.blur(limit); // request #1 submits "7000"
  await waitFor(() => expect(deferred.length).toBe(1));

  // The operator keeps typing while #1 is still in flight.
  fireEvent.change(limit, { target: { value: "8000" } });
  expect(limit.value).toBe("8000");

  // #1 settles. It must not drag the box back to what IT submitted.
  await act(async () => {
    deferred[0]({
      budgets: {
        threshold_pct: 90,
        notify: true,
        engines: { codex: { limit_tokens: 7000 } },
      },
      agents: [],
    });
  });
  expect(limit.value).toBe("8000");
});

test("a save the server REJECTS stops masking the stored value (#839)", async () => {
  // Distinct from the out-of-range case below, which never reaches the server: this value looks
  // fine to the client and is refused by the server. Keeping the draft would leave the field
  // asserting a number that was never persisted, discoverable only by reloading.
  vi.mocked(api.setAgentBudgets).mockRejectedValue(
    new Error(
      "agent_budgets.engines.codex.limit_tokens must be a whole number",
    ),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const limit = screen.getByRole("spinbutton", {
    name: /codex token limit/i,
  }) as HTMLInputElement;
  fireEvent.change(limit, { target: { value: "12345" } });
  fireEvent.blur(limit);

  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/limit_tokens/),
  );
  // Back to the stored value (unset here), not the refused one.
  await waitFor(() => expect(limit.value).toBe(""));
});

test("a numeric field is reconciled by the server, not left as typed (#839)", async () => {
  // The fields were uncontrolled, so nothing could correct them: a rejected save left the box
  // showing a number the server never accepted, with no way back.
  vi.mocked(api.setAgentBudgets).mockRejectedValue(
    new Error("agent_budgets rejected"),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());

  const input = screen.getByRole("spinbutton", {
    name: /alert threshold/i,
  }) as HTMLInputElement;
  // Out of range: the server would refuse it, so the panel snaps back rather than displaying it.
  fireEvent.change(input, { target: { value: "0" } });
  fireEvent.blur(input);
  await waitFor(() => expect(input.value).toBe("90"));
  expect(api.setAgentBudgets).not.toHaveBeenCalled();
});

test("a rejected budget save says why, in the server's words (#839)", async () => {
  vi.mocked(api.setAgentBudgets).mockRejectedValue(
    new Error(
      "agent_budgets.threshold_pct must be an integer between 1 and 100",
    ),
  );
  renderSettings("dark", "#ffb000", "/settings/agents");
  await waitFor(() => expect(screen.getByText("93%")).toBeInTheDocument());
  const input = screen.getByRole("spinbutton", { name: /alert threshold/i });
  fireEvent.change(input, { target: { value: "55" } });
  fireEvent.blur(input);
  // "Could not save" would leave the operator guessing which field the server disliked (#834).
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/threshold_pct/),
  );
});

test("renders the Host section with humanized fields", async () => {
  renderSettings("dark", "#ffb000", "/settings/system");
  expect(screen.getByRole("heading", { name: "Host" })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByText("Linux 6.8.0")).toBeInTheDocument(),
  );
  // CPU + load, humanized memory (8/16 GB used/total), humanized uptime (90000s = 1d 1h)
  expect(screen.getByText(/8 cores · load 0\.50/)).toBeInTheDocument();
  expect(screen.getByText("8.0 GB / 16 GB")).toBeInTheDocument();
  expect(screen.getByText("1d 1h")).toBeInTheDocument();
});

test("Updates: check finds an update, then apply calls the API", async () => {
  vi.mocked(api.updateCheck).mockResolvedValue({
    current: "0.0.1",
    channel: "main",
    latest: "abc1234",
    update_available: true,
  });
  vi.mocked(api.updateApply).mockResolvedValue({ status: "updating" });
  renderSettings("dark", "#ffb000", "/settings/updates");
  await userEvent.click(
    screen.getByRole("button", { name: /check for updates/i }),
  );
  expect(
    await screen.findByText(/update available: abc1234/i),
  ).toBeInTheDocument();
  vi.mocked(api.updateProgress).mockResolvedValue({
    state: "running",
    steps: 7,
    step: "fetch",
    step_index: 2,
    label: "Downloading the release",
    started_at: Math.floor(Date.now() / 1000) - 5,
    elapsed_s: 5,
  });
  await userEvent.click(screen.getByRole("button", { name: /update now/i }));
  expect(api.updateApply).toHaveBeenCalled();
  // #1085: the card shows the installer's own progress instead of "reload in a moment".
  expect(await screen.findByTestId("update-progress")).toHaveTextContent(
    "Step 2 of 7 · Downloading the release",
  );
});

// ---- Updates: the frozen-install escape hatch (#931) ----

test("Updates: the apply action exists even when no update is available (#931)", async () => {
  // THE FREEZE. `update_available: false` used to hide the only control that could move the
  // install, so a wrong verdict was terminal: nothing to press, and nothing saying why. The
  // action is offered regardless — and named for what it actually does, because the installer
  // rebuilds and restarts rather than no-opping (#932).
  vi.mocked(api.updateCheck).mockResolvedValue({
    current: "0.19.2",
    channel: "main",
    latest: "12b14b6",
    update_available: false,
  });
  vi.mocked(api.updateApply).mockResolvedValue({ status: "updating" });
  renderSettings("dark", "#ffb000", "/settings/updates");
  await userEvent.click(
    screen.getByRole("button", { name: /check for updates/i }),
  );
  await screen.findByText(/you.re on the latest \(12b14b6\)/i);

  // Present, honestly labelled, and it REACHES apply — a rendered-but-inert button would
  // satisfy a visibility assertion and leave the install exactly as stuck.
  const apply = await screen.findByTestId("update-apply");
  expect(apply).toHaveTextContent(/reinstall latest/i);
  expect(screen.getByTestId("update-restart-cost")).toHaveTextContent(
    /restarts the service/i,
  );
  await userEvent.click(apply);
  expect(api.updateApply).toHaveBeenCalled();
});

test("Updates: an undetermined verdict never reads as up to date (#931)", async () => {
  // The partial failure: main HEAD resolved, the running build's own tag did not. Reporting
  // that as "You're on the latest" is the reassurance that let an install sit 26 days behind.
  vi.mocked(api.updateCheck).mockResolvedValue({
    current: "0.19.2",
    channel: "main",
    latest: "12b14b6",
    update_available: false,
    undetermined: true,
  });
  renderSettings("dark", "#ffb000", "/settings/updates");
  await userEvent.click(
    screen.getByRole("button", { name: /check for updates/i }),
  );
  expect(
    await screen.findByTestId("update-undetermined"),
  ).toHaveTextContent(/couldn.t determine whether an update is available/i);
  expect(screen.queryByText(/you.re on the latest/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/you.re up to date/i)).not.toBeInTheDocument();
  // Still reachable: an uncertain verdict is exactly when the operator needs the way out.
  expect(await screen.findByTestId("update-apply")).toBeInTheDocument();
});

test("Updates: the apply action is disabled while a reinstall is running (#931)", async () => {
  vi.mocked(api.updateCheck).mockResolvedValue({
    current: "0.19.2",
    channel: "main",
    latest: "12b14b6",
    update_available: false,
  });
  let release!: (v: { status: string }) => void;
  vi.mocked(api.updateApply).mockReturnValue(
    new Promise((res) => {
      release = res;
    }),
  );
  renderSettings("dark", "#ffb000", "/settings/updates");
  await userEvent.click(
    screen.getByRole("button", { name: /check for updates/i }),
  );
  const apply = await screen.findByTestId("update-apply");
  await userEvent.click(apply);
  await waitFor(() => expect(apply).toBeDisabled());
  expect(apply).toHaveTextContent(/updating/i);
  release({ status: "updating" });
});

// ---- Updates: in-app auto-update settings (#538) ----

test("Updates: automatic-updates toggle loads from settings and persists", async () => {
  renderSettings("dark", "#ffb000", "/settings/updates");
  const toggle = await screen.findByRole("checkbox", {
    name: /automatic updates/i,
  });
  await waitFor(() => expect(toggle).toBeEnabled()); // enabled once settings load
  expect(toggle).not.toBeChecked(); // default off (opt-in preserved)
  await userEvent.click(toggle);
  expect(api.setUpdateSettings).toHaveBeenCalledWith({ auto_update: true });
  await waitFor(() => expect(toggle).toBeChecked());
  // With auto-update on and no pass yet this run, the recent-runtime status line shows.
  expect(
    screen.getByText(/no automatic check yet since the last restart/i),
  ).toBeInTheDocument();
});

test("Updates: last automatic check renders as recent runtime status", async () => {
  vi.mocked(api.updateSettings).mockResolvedValue({
    auto_update: true,
    channel: "stable",
    last_auto: { ts: 1720000000, result: "up-to-date" },
  });
  renderSettings("dark", "#ffb000", "/settings/updates");
  expect(
    await screen.findByText(/last automatic check: .*up-to-date/i),
  ).toBeInTheDocument();
});

test("Updates: switching channel drops a stale in-flight check result", async () => {
  // Hermes #539 race: a check started under the OLD channel must not repopulate the
  // "update available" line after the user switches channels.
  let resolveCheck!: (v: {
    current: string;
    channel: string;
    latest: string;
    update_available: boolean;
  }) => void;
  vi.mocked(api.updateCheck).mockReturnValue(
    new Promise((res) => {
      resolveCheck = res;
    }),
  );
  renderSettings("dark", "#ffb000", "/settings/updates");
  const main = await screen.findByRole("radio", { name: /main/i });
  await waitFor(() => expect(main).toBeEnabled());
  await userEvent.click(
    screen.getByRole("button", { name: /check for updates/i }),
  );
  await userEvent.click(main); // switch channels while the check is still in flight
  resolveCheck({
    current: "0.0.1",
    channel: "stable",
    latest: "v9.9.9",
    update_available: true,
  });
  await flushFetches();
  expect(screen.queryByText(/update available/i)).not.toBeInTheDocument();
});

test("Updates: release-channel radiogroup persists the channel", async () => {
  renderSettings("dark", "#ffb000", "/settings/updates");
  const main = await screen.findByRole("radio", { name: /main/i });
  const stable = screen.getByRole("radio", { name: /stable/i });
  await waitFor(() => expect(main).toBeEnabled());
  expect(stable).toHaveAttribute("aria-checked", "true");
  await userEvent.click(main);
  expect(api.setUpdateSettings).toHaveBeenCalledWith({ channel: "main" });
  await waitFor(() => expect(main).toHaveAttribute("aria-checked", "true"));
  expect(stable).toHaveAttribute("aria-checked", "false");
});

// ---- Security tab ----

test("2FA: enable flow shows QR + manual key + recovery codes, then confirms", async () => {
  vi.mocked(api.enroll2fa).mockResolvedValue({
    secret: "JBSWY3DPEHPK3PXP",
    otpauth_uri:
      "otpauth://totp/BattleLab:marcus?secret=JBSWY3DPEHPK3PXP&issuer=BattleLab",
    recovery_codes: ["aaaa-bbbb-cccc", "dddd-eeee-ffff"],
  });
  vi.mocked(api.confirm2fa).mockResolvedValue(undefined);
  renderSettings("dark", "#ffb000", "/settings/security");
  expect(
    await screen.findByRole("heading", { name: /two-factor authentication/i }),
  ).toBeInTheDocument();

  await userEvent.click(
    screen.getByRole("button", { name: /enable two-factor auth/i }),
  );
  // Manual key + recovery codes are shown.
  expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
  expect(screen.getByText("aaaa-bbbb-cccc")).toBeInTheDocument();
  expect(screen.getByAltText(/qr code/i)).toBeInTheDocument();

  await userEvent.type(screen.getByPlaceholderText(/6-digit code/i), "123456");
  await userEvent.click(
    screen.getByRole("button", { name: /confirm & enable/i }),
  );
  expect(api.confirm2fa).toHaveBeenCalledWith("123456");
  expect(
    await screen.findByText(/two-factor authentication is on/i),
  ).toBeInTheDocument();
});

test("2FA: hidden entirely when auth_mode is none", async () => {
  // Decided once, by SecurityPanel from the shared config (#682). The card's own second
  // auth_mode check was dead code and went in #956, so the test drives the shared config.
  renderSettings("dark", "#ffb000", "/settings/security", "none");
  expect(screen.getByRole("heading", { name: /^login$/i })).toBeInTheDocument();
  await flushFetches();
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: /two-factor authentication/i }),
    ).toBeNull(),
  );
});

test("Account: Sign out calls the logout API (#141)", async () => {
  renderSettings("dark", "#ffb000", "/settings/security");
  const btn = await screen.findByRole("button", { name: /sign out/i });
  await userEvent.click(btn);
  expect(api.logout).toHaveBeenCalled();
});

test("Account: Sign out hidden when auth_mode is none (#141)", async () => {
  renderSettings("dark", "#ffb000", "/settings/security", "none");
  await flushFetches();
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull(),
  );
});

// ---- Projects tab ----

test("Session overview: unticking a project hides it + persists via projects_hidden (#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/beta", label: "Beta" },
    ],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // Inverse semantics (#174): the row starts CHECKED (visible). Unticking hides.
  const alpha = await screen.findByRole("checkbox", { name: /~\/alpha/i });
  expect(alpha).toBeChecked();
  await userEvent.click(alpha);
  expect(api.setPrefs).toHaveBeenCalledWith({
    projects_hidden: ["/home/u/alpha"],
  });
  // Shared state updates → the row immediately reflects the hidden state (now unchecked).
  expect(
    await screen.findByRole("checkbox", { name: /~\/alpha/i }),
  ).not.toBeChecked();
});

test("Session overview: renaming via the modal persists via setProjectName (#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // Click the project NAME (a button now, not an inline input) → opens the rename modal.
  const trigger = await screen.findByRole("button", {
    name: /rename ~\/alpha/i,
  });
  await userEvent.click(trigger);
  const modalInput = await screen.findByRole("textbox", {
    name: /custom name for \/home\/u\/alpha/i,
  });
  await userEvent.type(modalInput, "My Alpha");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({
    project_names: { "/home/u/alpha": "My Alpha" },
  });
});

test("Session overview: a name seeded after /api/config resolves is shown on the row (#161/#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  // OverviewPrefs seeds projectNames from ConfigCtx, which is null until /api/config resolves —
  // and the row can mount first. Start with null config, then deliver it with a saved name and
  // assert the row's clickable name reflects it (post-#174 the name lives on the button label,
  // not in an inline input).
  const seeded: AppConfig = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "single-user",
    two_factor_enabled: false,
    project_names: { "/home/u/alpha": "Saved Alpha" },
  };
  const tree = (cfg: AppConfig | null) => (
    <MemoryRouter initialEntries={["/settings/projects"]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <ConfigCtx.Provider value={cfg}>
          <OverviewPrefsProvider>
            <Routes>
              <Route path="/settings/:tab" element={<Settings />} />
            </Routes>
          </OverviewPrefsProvider>
        </ConfigCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>
  );
  const { rerender } = render(tree(null));
  // Before the name arrives, the row shows the shortened path as its visible label.
  await screen.findByRole("button", { name: /rename ~\/alpha/i });
  expect(screen.queryByText("Saved Alpha")).not.toBeInTheDocument();
  rerender(tree(seeded));
  // The name shows on the row's rename button (it ALSO labels the default-project
  // option since #357 Phase 2, so scope to the button rather than a bare text query).
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /rename ~\/alpha/i }),
    ).toHaveTextContent("Saved Alpha"),
  );
});

// ---- Session overview: entity-grouped rendering (#465) ----

test("Session overview: folders are grouped under their owning entity + Unassigned (#465)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/loose", label: "Loose" },
    ],
  });
  // One entity owning /home/u/alpha; /home/u/loose is owned by nobody → Unassigned.
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      {
        id: "p-1",
        name: "Alpha Project",
        color: "#c02020",
        folders: ["/home/u/alpha"],
        default_folder: "/home/u/alpha",
        archived: false,
        created_at: 0,
        session_count: 1,
      },
    ],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // The overview renders one per-entity group (its list is uniquely labelled "Folders in
  // <name>") + an "Unassigned" group — distinct from the ProjectsManager's own entity list above.
  const alphaList = await screen.findByRole("list", {
    name: /folders in alpha project/i,
  });
  const unassignedList = screen.getByRole("list", {
    name: /folders in unassigned/i,
  });
  // Both folders keep the inverse-checkbox.
  expect(screen.getByRole("checkbox", { name: /~\/alpha/i })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: /~\/loose/i })).toBeChecked();
  // Rename is only for the UNADOPTED folder (#615 Phase 3): ~/loose (under Unassigned) has the
  // button; ~/alpha (adopted by Alpha Project) does not — its name comes from the project.
  expect(unassignedList).toContainElement(
    screen.getByRole("button", { name: /rename ~\/loose/i }),
  );
  expect(
    screen.queryByRole("button", { name: /rename ~\/alpha/i }),
  ).not.toBeInTheDocument();
  // The adopted row still shows its path, just as static text.
  expect(alphaList).toHaveTextContent("~/alpha");
  await flushFetches();
});

test("Session overview: an adopted folder has no rename control; an unassigned one does (#615 Phase 3)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/loose", label: "Loose" },
    ],
  });
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      {
        id: "p-1",
        name: "Alpha Project",
        color: "#c02020",
        folders: ["/home/u/alpha"],
        default_folder: "/home/u/alpha",
        archived: false,
        created_at: 0,
        session_count: 1,
      },
    ],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // Adopted: no rename button, and the static name carries the "rename the project" hint.
  await screen.findByRole("checkbox", {
    name: "Offer ~/alpha as a launch location",
  });
  expect(
    screen.queryByRole("button", { name: /rename ~\/alpha/i }),
  ).not.toBeInTheDocument();
  expect(screen.getByTitle(/named by its project/i)).toHaveTextContent(
    "~/alpha",
  );
  // Unadopted: the rename button is still offered.
  expect(
    screen.getByRole("button", { name: /rename ~\/loose/i }),
  ).toBeInTheDocument();
  await flushFetches();
});

test("Session overview: the checkbox label states what unticking does, per row kind (#615)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/loose", label: "Loose" },
    ],
  });
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      {
        id: "p-1",
        name: "Alpha Project",
        color: "#c02020",
        folders: ["/home/u/alpha"],
        default_folder: "/home/u/alpha",
        archived: false,
        created_at: 0,
        session_count: 1,
      },
    ],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // Adopted: unticking only withholds the folder as a launch location — the project's
  // sessions are exempt server-side (`sessions.py` `_visible`), so the old "hide it
  // everywhere" promise never held here.
  expect(
    await screen.findByRole("checkbox", {
      name: "Offer ~/alpha as a launch location",
    }),
  ).toBeChecked();
  // Unadopted: unticking really does drop it from the sidebar/filter/overview too.
  expect(
    screen.getByRole("checkbox", {
      name: "Show ~/loose in the sidebar, filter, and overview",
    }),
  ).toBeChecked();
  // And the card no longer claims a blanket "hide it everywhere".
  expect(screen.queryByText(/hide it everywhere/i)).not.toBeInTheDocument();
  await flushFetches();
});

// ---- Folder discovery card (#465) ----

test("Folder discovery: shows configured roots/exclusions and removing a root persists (#465)", async () => {
  function renderDiscovery(config: Partial<AppConfig> = {}) {
    const cfg: AppConfig = {
      csrf: "t",
      new_session_engines: [],
      terminal_backend: "ws",
      auth_mode: "single-user",
      two_factor_enabled: false,
      ...config,
    };
    render(
      <MemoryRouter initialEntries={["/settings/projects"]}>
        <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
          <ConfigCtx.Provider value={cfg}>
            <OverviewPrefsProvider>
              <Routes>
                <Route path="/settings/:tab" element={<Settings />} />
              </Routes>
            </OverviewPrefsProvider>
          </ConfigCtx.Provider>
        </ThemeCtx.Provider>
      </MemoryRouter>,
    );
  }
  renderDiscovery({
    project_roots: ["/home/u/code"],
    folder_exclusions: ["/home/u/code/scratch"],
  });
  expect(
    await screen.findByRole("heading", { name: /folder discovery/i }),
  ).toBeInTheDocument();
  // The configured root + exclusion render with Remove buttons.
  const rootList = screen.getByRole("list", { name: /root directories/i });
  expect(rootList).toBeInTheDocument();
  expect(
    screen.getByRole("list", { name: /excluded folders/i }),
  ).toBeInTheDocument();
  // Removing the root persists the empty list via setPrefs.
  await userEvent.click(
    screen.getByRole("button", { name: /remove root ~\/code/i }),
  );
  expect(api.setPrefs).toHaveBeenCalledWith({ project_roots: [] });
  await flushFetches();
});

test("Folder discovery: picking a root through the folder picker commits it (#465)", async () => {
  function renderDiscovery() {
    const cfg: AppConfig = {
      csrf: "t",
      new_session_engines: [],
      terminal_backend: "ws",
      auth_mode: "single-user",
      two_factor_enabled: false,
    };
    render(
      <MemoryRouter initialEntries={["/settings/projects"]}>
        <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
          <ConfigCtx.Provider value={cfg}>
            <OverviewPrefsProvider>
              <Routes>
                <Route path="/settings/:tab" element={<Settings />} />
              </Routes>
            </OverviewPrefsProvider>
          </ConfigCtx.Provider>
        </ThemeCtx.Provider>
      </MemoryRouter>,
    );
  }
  // The server echoes the effective list on commit.
  vi.mocked(api.setPrefs).mockResolvedValue({ project_roots: ["/home/u"] });
  renderDiscovery();
  // Open the root picker, then "Select ~" (home) returns the home path → committed as a root.
  await userEvent.click(
    await screen.findByRole("button", { name: /add root…/i }),
  );
  const select = await screen.findByRole("button", { name: /^select ~$/i });
  await userEvent.click(select);
  expect(api.setPrefs).toHaveBeenCalledWith({ project_roots: ["/home/u"] });
  await flushFetches();
});

// ---- Default project (#335 Phase 2, surfaced in #357 Phase 2) ----

/** Mounts the Projects tab with a real ConfigCtx value so the picker can seed from
 *  `config.default_project` (renderSettings has no config provider). */
function renderProjectsTab(config: Partial<AppConfig> = {}) {
  const cfg: AppConfig = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "single-user",
    two_factor_enabled: false,
    ...config,
  };
  render(
    <MemoryRouter initialEntries={["/settings/projects"]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <ConfigCtx.Provider value={cfg}>
          <OverviewPrefsProvider>
            <Routes>
              <Route path="/settings/:tab" element={<Settings />} />
            </Routes>
          </OverviewPrefsProvider>
        </ConfigCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
}

test("Default project: the card is gone — the star in Projects owns it now (#615 Phase 2)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  renderProjectsTab({ default_project: "/home/u/alpha" });
  await screen.findByRole("heading", { name: "Projects" });
  // The cwd-valued picker is retired: `entity.default_folder` (#448) shadowed it, and the
  // project it pre-selected was `entities[0]` — alphabetical and unsettable.
  expect(
    screen.queryByRole("combobox", { name: "Default project" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Default project" }),
  ).not.toBeInTheDocument();
  // Nothing writes the legacy pref from Settings any more.
  expect(api.setPrefs).not.toHaveBeenCalledWith(
    expect.objectContaining({ default_project: expect.anything() }),
  );
  await flushFetches();
});

// ---- Discovery-scope live refresh (#470) ----

/** A rerenderable Projects-tab tree so tests can deliver a NEW config value the way a
 *  FolderDiscoveryCard save does (setPrefs → useConfigRefresh → fresh /api/config). */
function projectsTree(cfg: AppConfig) {
  return (
    <MemoryRouter initialEntries={["/settings/projects"]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <ConfigCtx.Provider value={cfg}>
          <OverviewPrefsProvider>
            <Routes>
              <Route path="/settings/:tab" element={<Settings />} />
            </Routes>
          </OverviewPrefsProvider>
        </ConfigCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>
  );
}

const baseProjectsCfg: AppConfig = {
  csrf: "t",
  new_session_engines: [],
  terminal_backend: "ws",
  auth_mode: "single-user",
  two_factor_enabled: false,
};

test("Session overview refetches /api/folders when the discovery scope changes (#470)", async () => {
  const { rerender } = render(
    projectsTree({ ...baseProjectsCfg, project_roots: [] }),
  );
  await screen.findByRole("heading", { name: /session overview/i });
  await flushFetches();
  // Mount: OverviewCard fetches all folders; the ProjectsManager card (out of scope for #470 —
  // mount-only) fetches its adoption list. The DefaultProjectCard's visible-set fetch is gone
  // with the card (#615 Phase 2), so this is 2, not 3.
  const before = vi.mocked(api.folders).mock.calls.length;
  expect(before).toBe(2);
  // A roots change lands in config (FolderDiscoveryCard save → config refresh) → OverviewCard
  // refetches. Only it keys on the discovery scope now.
  rerender(
    projectsTree({ ...baseProjectsCfg, project_roots: ["/home/u/code"] }),
  );
  await waitFor(() =>
    expect(vi.mocked(api.folders).mock.calls.length).toBe(before + 1),
  );
  // …and an exclusions change refetches again.
  rerender(
    projectsTree({
      ...baseProjectsCfg,
      project_roots: ["/home/u/code"],
      folder_exclusions: ["/home/u/code/scratch"],
    }),
  );
  await waitFor(() =>
    expect(vi.mocked(api.folders).mock.calls.length).toBe(before + 2),
  );
  await flushFetches();
});

// ---- Back link (#155) ----

test.each([
  ["/s/claude/abc", "/s/claude/abc"], // an in-app session path is honored
  ["/overview", "/overview"], // any internal path is fine
  [undefined, "/"], // opened directly (no state) → landing
  ["//evil.example", "/"], // protocol-relative → rejected
  ["https://evil.example", "/"], // absolute external → rejected
  ["/settings", "/"], // self → rejected (no loop)
  ["/settings/about", "/"], // self (tab URL) → rejected too (#357)
])(
  "Settings back link honors only internal returnTo: %s → %s (#155)",
  (returnTo, expected) => {
    render(
      <MemoryRouter
        initialEntries={[
          { pathname: "/settings/appearance", state: returnTo && { returnTo } },
        ]}
      >
        <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
          <OverviewPrefsProvider>
            <Routes>
              <Route path="/settings/:tab" element={<Settings />} />
            </Routes>
          </OverviewPrefsProvider>
        </ThemeCtx.Provider>
      </MemoryRouter>,
    );
    expect(
      screen.getByRole("link", { name: "Back to sessions" }),
    ).toHaveAttribute("href", expected);
  },
);

test("the #155 returnTo survives the bare-/settings redirect and tab switches", async () => {
  render(
    <MemoryRouter
      initialEntries={[
        { pathname: "/settings", state: { returnTo: "/s/claude/x" } },
      ]}
    >
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <OverviewPrefsProvider>
          <Routes>
            <Route path="/settings" element={<Settings />} />
            <Route path="/settings/:tab" element={<Settings />} />
          </Routes>
          <LocationProbe />
        </OverviewPrefsProvider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  // Redirected to the first tab — the back link still points at the originating session.
  expect(screen.getByTestId("location")).toHaveTextContent(
    "/settings/appearance",
  );
  expect(
    screen.getByRole("link", { name: "Back to sessions" }),
  ).toHaveAttribute("href", "/s/claude/x");
  // Switch sections — the state rides along, so the back link keeps working.
  await userEvent.click(screen.getByRole("link", { name: "About" }));
  expect(
    screen.getByRole("link", { name: "Back to sessions" }),
  ).toHaveAttribute("href", "/s/claude/x");
});

// ---- About tab ----

test("About: version, license, creator link, and a safe coffee link", async () => {
  renderSettings("dark", "#ffb000", "/settings/about");
  await waitFor(() =>
    expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0),
  );

  // AGPL-3.0 §13 makes the license + source offer load-bearing for a network-served build,
  // so both links are asserted rather than left to drift with a copy edit.
  const license = screen.getByRole("link", { name: "AGPL-3.0-or-later" });
  expect(license).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions/blob/main/LICENSE",
  );
  expect(license).toHaveAttribute("target", "_blank");
  expect(license).toHaveAttribute("rel", "noopener noreferrer");

  const source = screen.getByRole("link", { name: /source code/i });
  expect(source).toHaveAttribute(
    "href",
    "https://github.com/teriansilva/agent-sessions",
  );
  expect(source).toHaveAttribute("rel", "noopener noreferrer");

  const link = screen.getByRole("link", { name: "Marcus Braun" });
  expect(link).toHaveAttribute("href", "https://superstatus.io");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");

  const coffee = screen.getByRole("link", { name: /buy me a coffee/i });
  expect(coffee).toHaveAttribute(
    "href",
    "https://buymeacoffee.com/teriansilva",
  );
  expect(coffee).toHaveAttribute("target", "_blank");
  expect(coffee).toHaveAttribute("rel", "noopener noreferrer");
});

// ---- Maintenance tab ----

test("Maintenance: archive-older confirms then calls the API with the chosen hours (#142)", async () => {
  vi.mocked(api.archiveOlder).mockResolvedValue({ archived: 2, skipped: 1 });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  // Default age is 168h; first click reveals the confirm step (no API call yet).
  await userEvent.click(
    await screen.findByRole("button", { name: /archive older/i }),
  );
  expect(api.archiveOlder).not.toHaveBeenCalled();
  await userEvent.click(
    screen.getByRole("button", { name: /confirm archive/i }),
  );
  expect(api.archiveOlder).toHaveBeenCalledWith(168);
  expect(
    await screen.findByText(/archived 2 sessions \(1 skipped\)\./i),
  ).toBeInTheDocument();
});

test("Maintenance: cancel backs out without archiving (#142)", async () => {
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  await userEvent.click(
    await screen.findByRole("button", { name: /archive older/i }),
  );
  await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(api.archiveOlder).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: /archive older/i }),
  ).toBeInTheDocument();
});

test("Scrollback cache: shows size and clears all after confirm (#206)", async () => {
  vi.mocked(api.scrollbackInfo).mockResolvedValue({
    bytes: 2 * 1024 * 1024,
    files: 3,
  });
  vi.mocked(api.clearScrollback).mockResolvedValue({
    scope: "all",
    removed: 3,
    bytes_freed: 2 * 1024 * 1024,
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  // The fetched cache size is shown.
  expect(
    await screen.findByText(/2(\.0)?\s?MB across 3 sessions/i),
  ).toBeInTheDocument();
  // First click reveals the confirm step (no API call yet).
  await userEvent.click(
    screen.getByRole("button", { name: /clear all cache/i }),
  );
  expect(api.clearScrollback).not.toHaveBeenCalled();
  await userEvent.click(
    screen.getByRole("button", { name: /confirm clear all/i }),
  );
  expect(api.clearScrollback).toHaveBeenCalledWith("all");
  expect(await screen.findByText(/cleared 3 cache files/i)).toBeInTheDocument();
});

test("Scrollback cache: clear archived passes the archived scope (#206)", async () => {
  vi.mocked(api.scrollbackInfo).mockResolvedValue({ bytes: 0, files: 0 });
  vi.mocked(api.clearScrollback).mockResolvedValue({
    scope: "archived",
    removed: 1,
    bytes_freed: 10,
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  await userEvent.click(
    await screen.findByRole("button", { name: /clear archived sessions/i }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: /confirm clear archived/i }),
  );
  expect(api.clearScrollback).toHaveBeenCalledWith("archived");
});

// ---- Maintenance: Archive old missions + Prune (#993) ----

test("Archive old missions: the confirm names the side effects, then reports skips and failures (#993)", async () => {
  vi.mocked(api.archiveOldMissionsInfo).mockResolvedValue({
    eligible: 9,
    sessions: 23,
    live_sessions: 5,
    unresolved: ["m1"],
    runner: null,
  });
  vi.mocked(api.archiveOldMissions).mockResolvedValue({
    archived: 8,
    sessions_archived: 22,
    terminals_stopped: 5,
    skipped: [{ mission_id: "m1", reason: "unresolved turn" }],
    failed: [{ mission_id: "m2", session_key: "claude:abc", reason: "background agent" }],
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  await userEvent.click(
    await screen.findByRole("button", { name: /archive old missions \(9\)/i }),
  );
  expect(api.archiveOldMissions).not.toHaveBeenCalled();
  expect(
    screen.getByText(/5 live terminals will be stopped; transcripts are kept/i),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/1 mission with an unresolved turn will be skipped/i),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: /confirm mission archive/i }),
  );
  expect(api.archiveOldMissions).toHaveBeenCalledWith(30);
  expect(
    await screen.findByText(/archived 8 missions and 22 of their sessions; stopped 5 live terminals/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/skipped mission m1: unresolved turn/i)).toBeInTheDocument();
  expect(
    screen.getByText(/session claude:abc was not archived \(mission m2 is archived\)/i),
  ).toBeInTheDocument();
});

test("Archive old missions: a busy runner is refused with retry copy (#993)", async () => {
  const { ApiError } = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  vi.mocked(api.archiveOldMissionsInfo).mockResolvedValue({
    eligible: 2,
    sessions: 2,
    live_sessions: 0,
    unresolved: [],
    runner: null,
  });
  vi.mocked(api.archiveOldMissions).mockRejectedValue(
    new ApiError(409, "busy", { busy: { job: "prune", started_at: 1 } }),
  );
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  await userEvent.click(
    await screen.findByRole("button", { name: /archive old missions \(2\)/i }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: /confirm mission archive/i }),
  );
  expect(
    await screen.findByText(/running \(prune\) — unavailable; retry when maintenance finishes/i),
  ).toBeInTheDocument();
});

test("Prune: dry-run counts render and the confirm names what is removed (#993)", async () => {
  vi.mocked(api.pruneInfo).mockResolvedValue({
    categories: {
      stale_sockets: { items: 159, bytes: 38912 },
      archived_scrollback: { items: 312, bytes: 188743680 },
    },
    runner: null,
  });
  vi.mocked(api.prune).mockResolvedValue({
    removed: 157,
    bytes_freed: 38400,
    skipped: [{ category: "stale_sockets", reason: "a live session holds its lock", count: 2 }],
    failed: [],
    failed_total: 0,
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  expect(await screen.findByText("159 · 38 KB")).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: /prune selected \(1\)/i }),
  );
  expect(api.prune).not.toHaveBeenCalled();
  expect(screen.getByText(/permanently remove 159 items/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /confirm prune/i }));
  expect(api.prune).toHaveBeenCalledWith(["stale_sockets"]);
  expect(await screen.findByText(/removed 157 items/i)).toBeInTheDocument();
  expect(
    screen.getByText(/skipped 2 \(stale terminal sockets\): a live session holds its lock/i),
  ).toBeInTheDocument();
});

test("Prune: a category that could not be measured blocks the run until it is deselected (#993)", async () => {
  vi.mocked(api.pruneInfo).mockResolvedValue({
    categories: {
      stale_sockets: { items: 0, bytes: 0, error: "OSError" },
      archived_scrollback: { items: 312, bytes: 188743680 },
    },
    runner: null,
  });
  vi.mocked(api.prune).mockResolvedValue({
    removed: 312,
    bytes_freed: 188743680,
    skipped: [],
    failed: [],
    failed_total: 0,
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  // The unmeasured category is selected by default, so the action is blocked rather than
  // submitting contents nobody counted.
  expect(await screen.findByText("couldn’t measure")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /prune selected/i })).toBeDisabled();
  expect(
    screen.getByText(/couldn’t be measured.*deselect it or refresh/i),
  ).toBeInTheDocument();
  // Deselecting it, and selecting the measured one, unblocks exactly what was counted.
  await userEvent.click(screen.getByRole("checkbox", { name: /stale terminal sockets/i }));
  await userEvent.click(screen.getByRole("checkbox", { name: /archived sessions’ scrollback/i }));
  await userEvent.click(screen.getByRole("button", { name: /prune selected \(1\)/i }));
  await userEvent.click(screen.getByRole("button", { name: /confirm prune/i }));
  expect(api.prune).toHaveBeenCalledWith(["archived_scrollback"]);
});

test("Prune: a dry run landing during confirmation cannot submit unknown contents (#993)", async () => {
  let landRefresh: (v: Awaited<ReturnType<typeof api.pruneInfo>>) => void = () => {};
  vi.mocked(api.pruneInfo)
    .mockResolvedValueOnce({
      categories: {
        stale_sockets: { items: 0, bytes: 0, error: "OSError" },
        archived_scrollback: { items: 312, bytes: 188743680 },
      },
      runner: null,
    })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          landRefresh = resolve;
        }),
    );
  renderSettings("dark", "#ffb000", "/settings/maintenance");

  // A Refresh is offered because one category could not be measured. Start it, then move the
  // selection to the measured category so the action unblocks while that GET is still in flight.
  expect(await screen.findByText("couldn’t measure")).toBeInTheDocument();
  const prune = screen.getByRole("heading", { name: "Prune" }).closest("section")!;
  await userEvent.click(within(prune).getByRole("button", { name: "Refresh the cache measurements" }));
  await userEvent.click(screen.getByRole("checkbox", { name: /stale terminal sockets/i }));
  await userEvent.click(screen.getByRole("checkbox", { name: /archived sessions’ scrollback/i }));
  await userEvent.click(within(prune).getByRole("button", { name: /prune selected \(1\)/i }));
  expect(screen.getByText(/permanently remove 312 items/i)).toBeInTheDocument();

  // The held dry run now lands, and it says the SELECTED category is unknown.
  landRefresh({
    categories: {
      stale_sockets: { items: 0, bytes: 0 },
      archived_scrollback: { items: 0, bytes: 0, error: "PermissionError" },
    },
    runner: null,
  });

  // The confirmation must not survive that, and nothing may be submitted on its strength.
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /confirm prune/i })).not.toBeInTheDocument(),
  );
  expect(screen.queryByText(/permanently remove 0 items/i)).not.toBeInTheDocument();
  expect(api.prune).not.toHaveBeenCalled();
  expect(within(prune).getByRole("button", { name: /prune selected/i })).toBeDisabled();
  expect(screen.getByText(/couldn’t be measured.*deselect it or refresh/i)).toBeInTheDocument();
});

test("Prune: a busy runner recovers to enabled once the job ends, without remounting (#993)", async () => {
  vi.mocked(api.pruneInfo)
    .mockResolvedValueOnce({
      categories: {
        stale_sockets: { items: 159, bytes: 38912 },
        archived_scrollback: { items: 0, bytes: 0 },
      },
      runner: { job: "missions", started_at: 1 },
    })
    .mockResolvedValue({
      categories: {
        stale_sockets: { items: 159, bytes: 38912 },
        archived_scrollback: { items: 0, bytes: 0 },
      },
      runner: null,
    });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  expect(
    await screen.findByText(/running \(missions\) — unavailable; retry when maintenance finishes/i),
  ).toBeInTheDocument();
  const prune = screen.getByRole("heading", { name: "Prune" }).closest("section")!;
  expect(within(prune).getByRole("button", { name: /prune selected/i })).toBeDisabled();
  // Refresh is always available while busy — the card recovers in place.
  await userEvent.click(within(prune).getByRole("button", { name: "Refresh the cache measurements" }));
  await waitFor(() =>
    expect(within(prune).getByRole("button", { name: /prune selected \(1\)/i })).toBeEnabled(),
  );
});

test("Prune: nothing to prune disables the action; a failed dry run offers Retry (#993)", async () => {
  vi.mocked(api.pruneInfo)
    .mockRejectedValueOnce(new Error("boom"))
    .mockResolvedValue({
      categories: {
        stale_sockets: { items: 0, bytes: 0 },
        archived_scrollback: { items: 0, bytes: 0 },
      },
      runner: null,
    });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  expect(
    await screen.findByText(/couldn’t measure the caches \(dry run failed\)/i),
  ).toBeInTheDocument();
  const prune = screen.getByRole("heading", { name: "Prune" }).closest("section")!;
  await userEvent.click(within(prune).getByRole("button", { name: "Refresh the cache measurements" }));
  expect(await screen.findByText("Nothing to prune right now.")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /prune selected \(1\)/i }),
  ).toBeDisabled();
});

// ---- session list order (#506) ----

test("the session-list order radios default to Recent activity (#506)", async () => {
  renderSettings("dark", "#ffb000", "/settings/session-defaults");
  expect(
    await screen.findByRole("radio", { name: /Recent activity/ }),
  ).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: /Creation date/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await flushFetches();
});

test("picking Creation date persists session_list_order via setPrefs (#506)", async () => {
  renderSettings("dark", "#ffb000", "/settings/session-defaults");
  await userEvent.click(
    await screen.findByRole("radio", { name: /Creation date/ }),
  );
  expect(api.setPrefs).toHaveBeenCalledWith({
    session_list_order: "created_at",
  });
  // Optimistic: the chosen card flips immediately.
  expect(screen.getByRole("radio", { name: /Creation date/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await flushFetches();
});

test("a failed session_list_order save rolls back the selection (#506)", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("nope"));
  renderSettings("dark", "#ffb000", "/settings/session-defaults");
  await userEvent.click(
    await screen.findByRole("radio", { name: /Creation date/ }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("radio", { name: /Recent activity/ }),
    ).toHaveAttribute("aria-checked", "true"),
  );
  await flushFetches();
});

// #548: the sidebar list re-sorts by watching the shared config's order, so the Settings
// radio must refresh the config after a successful save — and must NOT on a failed one.
test("a session_list_order save refreshes the shared config (#548)", async () => {
  const refresh = vi.fn();
  render(
    <MemoryRouter initialEntries={["/settings/session-defaults"]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <AccentCtx.Provider value={{ accent: "#ffb000", setAccent: vi.fn() }}>
          <ConfigRefreshCtx.Provider value={refresh}>
            <OverviewPrefsProvider>
              <Routes>
                <Route path="/settings/:tab" element={<Settings />} />
              </Routes>
            </OverviewPrefsProvider>
          </ConfigRefreshCtx.Provider>
        </AccentCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  await userEvent.click(
    await screen.findByRole("radio", { name: /Creation date/ }),
  );
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  await flushFetches();
});

test("a failed session_list_order save does NOT refresh the config (#548)", async () => {
  const refresh = vi.fn();
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("nope"));
  render(
    <MemoryRouter initialEntries={["/settings/session-defaults"]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <AccentCtx.Provider value={{ accent: "#ffb000", setAccent: vi.fn() }}>
          <ConfigRefreshCtx.Provider value={refresh}>
            <OverviewPrefsProvider>
              <Routes>
                <Route path="/settings/:tab" element={<Settings />} />
              </Routes>
            </OverviewPrefsProvider>
          </ConfigRefreshCtx.Provider>
        </AccentCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  await userEvent.click(
    await screen.findByRole("radio", { name: /Creation date/ }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("radio", { name: /Recent activity/ }),
    ).toHaveAttribute("aria-checked", "true"),
  );
  expect(refresh).not.toHaveBeenCalled();
  await flushFetches();
});
