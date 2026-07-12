import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { OverviewPrefsProvider } from "../app/OverviewPrefsContext";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import type { ThemeId } from "../theme/themes";
import { ThemeCtx } from "../theme/themeStore";
import { AccentCtx } from "../theme/accentStore";
import { Settings } from "./Settings";
import { SETTINGS_TABS } from "./settingsTabs";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      version: vi.fn(),
      setTheme: vi.fn(),
      setAccent: vi.fn(),
      engines: vi.fn(),
      system: vi.fn(),
      updateCheck: vi.fn(),
      updateApply: vi.fn(),
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
    },
  };
});

/** Surfaces the live router location so tests can assert the /settings/:tab URL contract. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

/** Mounts Settings under the real route shapes (#357): bare /settings and /settings/:tab —
 *  both render the component; the bare/unknown forms replace-redirect to the first tab. */
function renderSettings(theme: ThemeId = "dark", accent = "#ffb000", initialPath = "/settings") {
  const setTheme = vi.fn();
  const setAccent = vi.fn();
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <ThemeCtx.Provider value={{ theme, setTheme }}>
        <AccentCtx.Provider value={{ accent, setAccent }}>
          <OverviewPrefsProvider>
            <Routes>
              <Route path="/settings" element={<Settings />} />
              <Route path="/settings/:tab" element={<Settings />} />
            </Routes>
            <LocationProbe />
          </OverviewPrefsProvider>
        </AccentCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  return { setTheme, setAccent };
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
  vi.mocked(api.version).mockResolvedValue({ version: "1.2.3" });
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "single-user",
    two_factor_enabled: false,
  });
  vi.mocked(api.engines).mockResolvedValue({
    engines: [
      { id: "claude", present: true, supports_new: true, bin: "/usr/local/bin/claude" },
      { id: "codex", present: false, supports_new: false, bin: null },
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
  vi.mocked(api.clearScrollback).mockResolvedValue({ scope: "all", removed: 0, bytes_freed: 0 });
  // AI Review tab (#356): no sessions excluded, model listing unsupported by default.
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [],
    next_offset: null,
    total: 0,
    facets: { projects: [], engines: [] },
  });
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: [] });
  vi.mocked(api.reviewExclude).mockResolvedValue({ id: "x", review_excluded: false });
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

// ---- Tab shell: routing + deep links (#357 Phase 1) ----

test("bare /settings redirects to the first tab (canonical /settings/:tab)", async () => {
  renderSettings("dark", "#ffb000", "/settings");
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/appearance");
  expect(screen.getByRole("tab", { name: "Appearance" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await flushFetches();
});

test("an unknown tab falls back to the first tab (no 404)", async () => {
  renderSettings("dark", "#ffb000", "/settings/launch-codes");
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/appearance");
  expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
  await flushFetches();
});

test("the tablist exposes all tabs with roving tabindex", async () => {
  renderSettings("dark", "#ffb000", "/settings/projects");
  const tablist = screen.getByRole("tablist", { name: "Settings sections" });
  const tabs = screen.getAllByRole("tab");
  expect(tablist).toBeInTheDocument();
  expect(tabs.map((t) => t.textContent)).toEqual([
    "Appearance",
    "Projects",
    "AI",
    "Security",
    "System",
    "Maintenance",
    "About",
  ]);
  // Roving tabindex: only the active tab is in the tab order.
  for (const t of tabs) {
    expect(t).toHaveAttribute("tabindex", t.textContent === "Projects" ? "0" : "-1");
  }
  await flushFetches();
});

test("clicking a tab navigates to its canonical URL and swaps the panel", async () => {
  renderSettings();
  await userEvent.click(screen.getByRole("tab", { name: "Maintenance" }));
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/maintenance");
  expect(screen.getByRole("tab", { name: "Maintenance" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(await screen.findByRole("heading", { name: "Maintenance" })).toBeInTheDocument();
  // The previous panel's content is gone.
  expect(screen.queryByRole("heading", { name: "Appearance" })).not.toBeInTheDocument();
});

test("arrow keys move + select tabs with wrap-around; Home/End jump (#357)", async () => {
  renderSettings("dark", "#ffb000", "/settings/appearance");
  screen.getByRole("tab", { name: "Appearance" }).focus();

  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "Projects" })).toHaveFocus();
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/projects");

  await userEvent.keyboard("{ArrowLeft}");
  expect(screen.getByRole("tab", { name: "Appearance" })).toHaveFocus();
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/appearance");

  // Wrap-around: ArrowLeft from the first tab lands on the last.
  await userEvent.keyboard("{ArrowLeft}");
  expect(screen.getByRole("tab", { name: "About" })).toHaveFocus();
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/about");

  await userEvent.keyboard("{Home}");
  expect(screen.getByRole("tab", { name: "Appearance" })).toHaveFocus();
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/appearance");

  await userEvent.keyboard("{End}");
  expect(screen.getByRole("tab", { name: "About" })).toHaveFocus();
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/about");
  await flushFetches();
});

test("the active panel is a labelled tabpanel wired to its tab", async () => {
  renderSettings("dark", "#ffb000", "/settings/security");
  const panel = screen.getByRole("tabpanel");
  expect(panel).toHaveAttribute("id", "settings-panel-security");
  expect(panel).toHaveAttribute("aria-labelledby", "settings-tab-security");
  expect(screen.getByRole("tab", { name: "Security" })).toHaveAttribute(
    "aria-controls",
    "settings-panel-security",
  );
  await flushFetches();
});

// Every existing settings control still has a home: each tab renders its sections (#357
// zero-behavioural-change guarantee — components moved, not changed).
test.each([
  ["appearance", ["Appearance"]],
  ["projects", ["Projects", "Session overview"]],
  ["ai-review", ["AI endpoint", "Session review", "Auto-sort projects"]],
  ["security", ["Two-factor authentication", "Account"]],
  ["system", ["Connected agents", "System", "Updates"]],
  ["maintenance", ["Maintenance", "Scrollback cache"]],
  ["about", ["Support", "About"]],
])("tab %s renders its sections: %s", async (tab, headings) => {
  renderSettings("dark", "#ffb000", `/settings/${tab}`);
  for (const h of headings) {
    expect(await screen.findByRole("heading", { name: h })).toBeInTheDocument();
  }
  await flushFetches();
});

test("the AI tab renders the restructured panel (endpoint key, review prompt, auto-sort)", async () => {
  renderSettings("dark", "#ffb000", "/settings/ai-review");
  expect(await screen.findByRole("heading", { name: "AI endpoint" })).toBeInTheDocument();
  expect(screen.getByLabelText(/API key/i)).toBeInTheDocument();
  await flushFetches();
});

test("mobile smoke (390px): all tabs stay reachable in the scrollable bar (#289/#357)", async () => {
  // jsdom does no layout — this is a shell-invariant smoke: at a phone-width viewport the
  // full data-driven tab set still renders inside the single scrollable tablist (CSS makes
  // it overflow-x: auto), and the System dl rows render without dropping values.
  window.innerWidth = 390;
  window.dispatchEvent(new Event("resize"));
  renderSettings("dark", "#ffb000", "/settings/system");
  const tablist = screen.getByRole("tablist", { name: "Settings sections" });
  expect(tablist).toBeInTheDocument();
  expect(screen.getAllByRole("tab")).toHaveLength(SETTINGS_TABS.length);
  // The long-value System rows (the #289 overflow culprits) are all present.
  await waitFor(() => expect(screen.getByText("Linux 6.8.0")).toBeInTheDocument());
  expect(screen.getByText("Linux-6.8.0-x86_64 · x86_64")).toBeInTheDocument();
});

// ---- Appearance tab ----

test("renders the themes and the theme radios", async () => {
  renderSettings();
  expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  for (const label of ["Dark", "Light"]) {
    expect(screen.getByRole("radio", { name: new RegExp(label) })).toBeInTheDocument();
  }
  await flushFetches();
});

test("the active theme is marked aria-checked", async () => {
  renderSettings("light");
  expect(screen.getByRole("radio", { name: /Light/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: /Dark/ })).toHaveAttribute("aria-checked", "false");
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
  expect(screen.getByRole("radio", { name: "Signal Red" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: "Amber" })).toHaveAttribute("aria-checked", "false");
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

test("renders the Connected agents section with each engine + new-session badge", async () => {
  renderSettings("dark", "#ffb000", "/settings/system");
  expect(screen.getByRole("heading", { name: "Connected agents" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("claude")).toBeInTheDocument());
  expect(screen.getByText("codex")).toBeInTheDocument();
  // present engine shows its resolved bin + a "can start new" badge
  expect(screen.getByText("/usr/local/bin/claude")).toBeInTheDocument();
  expect(screen.getByText(/can start new/i)).toBeInTheDocument();
  // absent engine shows "not found"
  expect(screen.getByText("not found")).toBeInTheDocument();
});

test("renders the System section with humanized fields", async () => {
  renderSettings("dark", "#ffb000", "/settings/system");
  expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("Linux 6.8.0")).toBeInTheDocument());
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
  renderSettings("dark", "#ffb000", "/settings/system");
  await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));
  expect(await screen.findByText(/update available: abc1234/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /update now/i }));
  expect(api.updateApply).toHaveBeenCalled();
  expect(await screen.findByText(/will restart/i)).toBeInTheDocument();
});

// ---- Updates: in-app auto-update settings (#538) ----

test("Updates: automatic-updates toggle loads from settings and persists", async () => {
  renderSettings("dark", "#ffb000", "/settings/system");
  const toggle = await screen.findByRole("checkbox", { name: /automatic updates/i });
  await waitFor(() => expect(toggle).toBeEnabled()); // enabled once settings load
  expect(toggle).not.toBeChecked(); // default off (opt-in preserved)
  await userEvent.click(toggle);
  expect(api.setUpdateSettings).toHaveBeenCalledWith({ auto_update: true });
  await waitFor(() => expect(toggle).toBeChecked());
  // With auto-update on and no pass yet this run, the recent-runtime status line shows.
  expect(screen.getByText(/no automatic check yet since the last restart/i)).toBeInTheDocument();
});

test("Updates: last automatic check renders as recent runtime status", async () => {
  vi.mocked(api.updateSettings).mockResolvedValue({
    auto_update: true,
    channel: "stable",
    last_auto: { ts: 1720000000, result: "up-to-date" },
  });
  renderSettings("dark", "#ffb000", "/settings/system");
  expect(await screen.findByText(/last automatic check: .*up-to-date/i)).toBeInTheDocument();
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
  renderSettings("dark", "#ffb000", "/settings/system");
  const main = await screen.findByRole("radio", { name: /main/i });
  await waitFor(() => expect(main).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));
  await userEvent.click(main); // switch channels while the check is still in flight
  resolveCheck({ current: "0.0.1", channel: "stable", latest: "v9.9.9", update_available: true });
  await flushFetches();
  expect(screen.queryByText(/update available/i)).not.toBeInTheDocument();
});

test("Updates: release-channel radiogroup persists the channel", async () => {
  renderSettings("dark", "#ffb000", "/settings/system");
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
    otpauth_uri: "otpauth://totp/BattleLab:marcus?secret=JBSWY3DPEHPK3PXP&issuer=BattleLab",
    recovery_codes: ["aaaa-bbbb-cccc", "dddd-eeee-ffff"],
  });
  vi.mocked(api.confirm2fa).mockResolvedValue(undefined);
  renderSettings("dark", "#ffb000", "/settings/security");
  expect(
    await screen.findByRole("heading", { name: /two-factor authentication/i }),
  ).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: /enable two-factor auth/i }));
  // Manual key + recovery codes are shown.
  expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
  expect(screen.getByText("aaaa-bbbb-cccc")).toBeInTheDocument();
  expect(screen.getByAltText(/qr code/i)).toBeInTheDocument();

  await userEvent.type(screen.getByPlaceholderText(/6-digit code/i), "123456");
  await userEvent.click(screen.getByRole("button", { name: /confirm & enable/i }));
  expect(api.confirm2fa).toHaveBeenCalledWith("123456");
  expect(await screen.findByText(/two-factor authentication is on/i)).toBeInTheDocument();
});

test("2FA: hidden entirely when auth_mode is none", async () => {
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "none",
    two_factor_enabled: false,
  });
  renderSettings("dark", "#ffb000", "/settings/security");
  await flushFetches();
  await waitFor(() =>
    expect(screen.queryByRole("heading", { name: /two-factor authentication/i })).toBeNull(),
  );
});

test("Account: Sign out calls the logout API (#141)", async () => {
  renderSettings("dark", "#ffb000", "/settings/security");
  const btn = await screen.findByRole("button", { name: /sign out/i });
  await userEvent.click(btn);
  expect(api.logout).toHaveBeenCalled();
});

test("Account: Sign out hidden when auth_mode is none (#141)", async () => {
  vi.mocked(api.config).mockResolvedValue({
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "none",
    two_factor_enabled: false,
  });
  renderSettings("dark", "#ffb000", "/settings/security");
  await flushFetches();
  await waitFor(() => expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull());
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
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_hidden: ["/home/u/alpha"] });
  // Shared state updates → the row immediately reflects the hidden state (now unchecked).
  expect(await screen.findByRole("checkbox", { name: /~\/alpha/i })).not.toBeChecked();
});

test("Session overview: renaming via the modal persists via setProjectName (#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  renderSettings("dark", "#ffb000", "/settings/projects");
  // Click the project NAME (a button now, not an inline input) → opens the rename modal.
  const trigger = await screen.findByRole("button", { name: /rename ~\/alpha/i });
  await userEvent.click(trigger);
  const modalInput = await screen.findByRole("textbox", {
    name: /custom name for \/home\/u\/alpha/i,
  });
  await userEvent.type(modalInput, "My Alpha");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ project_names: { "/home/u/alpha": "My Alpha" } });
});

test("Session overview: a name seeded after /api/config resolves is shown on the row (#161/#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({ folders: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
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
    expect(screen.getByRole("button", { name: /rename ~\/alpha/i })).toHaveTextContent(
      "Saved Alpha",
    ),
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
  const alphaList = await screen.findByRole("list", { name: /folders in alpha project/i });
  const unassignedList = screen.getByRole("list", { name: /folders in unassigned/i });
  // Both folders keep the inverse-checkbox.
  expect(screen.getByRole("checkbox", { name: /~\/alpha/i })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: /~\/loose/i })).toBeChecked();
  // Rename is only for the UNADOPTED folder (#615 Phase 3): ~/loose (under Unassigned) has the
  // button; ~/alpha (adopted by Alpha Project) does not — its name comes from the project.
  expect(unassignedList).toContainElement(screen.getByRole("button", { name: /rename ~\/loose/i }));
  expect(screen.queryByRole("button", { name: /rename ~\/alpha/i })).not.toBeInTheDocument();
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
  await screen.findByRole("checkbox", { name: "Offer ~/alpha as a launch location" });
  expect(screen.queryByRole("button", { name: /rename ~\/alpha/i })).not.toBeInTheDocument();
  expect(screen.getByTitle(/named by its project/i)).toHaveTextContent("~/alpha");
  // Unadopted: the rename button is still offered.
  expect(screen.getByRole("button", { name: /rename ~\/loose/i })).toBeInTheDocument();
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
    await screen.findByRole("checkbox", { name: "Offer ~/alpha as a launch location" }),
  ).toBeChecked();
  // Unadopted: unticking really does drop it from the sidebar/filter/overview too.
  expect(
    screen.getByRole("checkbox", { name: "Show ~/loose in the sidebar, filter, and overview" }),
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
  expect(await screen.findByRole("heading", { name: /folder discovery/i })).toBeInTheDocument();
  // The configured root + exclusion render with Remove buttons.
  const rootList = screen.getByRole("list", { name: /root directories/i });
  expect(rootList).toBeInTheDocument();
  expect(screen.getByRole("list", { name: /excluded folders/i })).toBeInTheDocument();
  // Removing the root persists the empty list via setPrefs.
  await userEvent.click(screen.getByRole("button", { name: /remove root ~\/code/i }));
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
  await userEvent.click(await screen.findByRole("button", { name: /add root…/i }));
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
  expect(screen.queryByRole("combobox", { name: "Default project" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Default project" })).not.toBeInTheDocument();
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
  const { rerender } = render(projectsTree({ ...baseProjectsCfg, project_roots: [] }));
  await screen.findByRole("heading", { name: /session overview/i });
  await flushFetches();
  // Mount: OverviewCard fetches all folders; the ProjectsManager card (out of scope for #470 —
  // mount-only) fetches its adoption list. The DefaultProjectCard's visible-set fetch is gone
  // with the card (#615 Phase 2), so this is 2, not 3.
  const before = vi.mocked(api.folders).mock.calls.length;
  expect(before).toBe(2);
  // A roots change lands in config (FolderDiscoveryCard save → config refresh) → OverviewCard
  // refetches. Only it keys on the discovery scope now.
  rerender(projectsTree({ ...baseProjectsCfg, project_roots: ["/home/u/code"] }));
  await waitFor(() => expect(vi.mocked(api.folders).mock.calls.length).toBe(before + 1));
  // …and an exclusions change refetches again.
  rerender(
    projectsTree({
      ...baseProjectsCfg,
      project_roots: ["/home/u/code"],
      folder_exclusions: ["/home/u/code/scratch"],
    }),
  );
  await waitFor(() => expect(vi.mocked(api.folders).mock.calls.length).toBe(before + 2));
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
])("Settings back link honors only internal returnTo: %s → %s (#155)", (returnTo, expected) => {
  render(
    <MemoryRouter
      initialEntries={[{ pathname: "/settings/appearance", state: returnTo && { returnTo } }]}
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
  expect(screen.getByRole("link", { name: "Back to sessions" })).toHaveAttribute("href", expected);
});

test("the #155 returnTo survives the bare-/settings redirect and tab switches", async () => {
  render(
    <MemoryRouter initialEntries={[{ pathname: "/settings", state: { returnTo: "/s/claude/x" } }]}>
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
  expect(screen.getByTestId("location")).toHaveTextContent("/settings/appearance");
  expect(screen.getByRole("link", { name: "Back to sessions" })).toHaveAttribute(
    "href",
    "/s/claude/x",
  );
  // Switch tabs — the state rides along, so the back link keeps working.
  await userEvent.click(screen.getByRole("tab", { name: "About" }));
  expect(screen.getByRole("link", { name: "Back to sessions" })).toHaveAttribute(
    "href",
    "/s/claude/x",
  );
});

// ---- About tab ----

test("About: version, creator link, and a safe coffee link", async () => {
  renderSettings("dark", "#ffb000", "/settings/about");
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));

  const link = screen.getByRole("link", { name: "Marcus Braun" });
  expect(link).toHaveAttribute("href", "https://superstatus.io");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");

  const coffee = screen.getByRole("link", { name: /buy me a coffee/i });
  expect(coffee).toHaveAttribute("href", "https://buymeacoffee.com/teriansilva");
  expect(coffee).toHaveAttribute("target", "_blank");
  expect(coffee).toHaveAttribute("rel", "noopener noreferrer");
});

// ---- Maintenance tab ----

test("Maintenance: archive-older confirms then calls the API with the chosen hours (#142)", async () => {
  vi.mocked(api.archiveOlder).mockResolvedValue({ archived: 2, skipped: 1 });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  // Default age is 168h; first click reveals the confirm step (no API call yet).
  await userEvent.click(await screen.findByRole("button", { name: /archive older/i }));
  expect(api.archiveOlder).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: /confirm archive/i }));
  expect(api.archiveOlder).toHaveBeenCalledWith(168);
  expect(await screen.findByText(/archived 2 sessions \(1 skipped\)\./i)).toBeInTheDocument();
});

test("Maintenance: cancel backs out without archiving (#142)", async () => {
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  await userEvent.click(await screen.findByRole("button", { name: /archive older/i }));
  await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
  expect(api.archiveOlder).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /archive older/i })).toBeInTheDocument();
});

test("Scrollback cache: shows size and clears all after confirm (#206)", async () => {
  vi.mocked(api.scrollbackInfo).mockResolvedValue({ bytes: 2 * 1024 * 1024, files: 3 });
  vi.mocked(api.clearScrollback).mockResolvedValue({
    scope: "all",
    removed: 3,
    bytes_freed: 2 * 1024 * 1024,
  });
  renderSettings("dark", "#ffb000", "/settings/maintenance");
  // The fetched cache size is shown.
  expect(await screen.findByText(/2(\.0)?\s?MB across 3 sessions/i)).toBeInTheDocument();
  // First click reveals the confirm step (no API call yet).
  await userEvent.click(screen.getByRole("button", { name: /clear all cache/i }));
  expect(api.clearScrollback).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: /confirm clear all/i }));
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
  await userEvent.click(await screen.findByRole("button", { name: /clear archived sessions/i }));
  await userEvent.click(screen.getByRole("button", { name: /confirm clear archived/i }));
  expect(api.clearScrollback).toHaveBeenCalledWith("archived");
});

// ---- session list order (#506) ----

test("the session-list order radios default to Recent activity (#506)", async () => {
  renderSettings("dark", "#ffb000", "/settings/appearance");
  expect(await screen.findByRole("radio", { name: /Recent activity/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(screen.getByRole("radio", { name: /Creation date/ })).toHaveAttribute(
    "aria-checked",
    "false",
  );
  await flushFetches();
});

test("picking Creation date persists session_list_order via setPrefs (#506)", async () => {
  renderSettings("dark", "#ffb000", "/settings/appearance");
  await userEvent.click(await screen.findByRole("radio", { name: /Creation date/ }));
  expect(api.setPrefs).toHaveBeenCalledWith({ session_list_order: "created_at" });
  // Optimistic: the chosen card flips immediately.
  expect(screen.getByRole("radio", { name: /Creation date/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await flushFetches();
});

test("a failed session_list_order save rolls back the selection (#506)", async () => {
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("nope"));
  renderSettings("dark", "#ffb000", "/settings/appearance");
  await userEvent.click(await screen.findByRole("radio", { name: /Creation date/ }));
  await waitFor(() =>
    expect(screen.getByRole("radio", { name: /Recent activity/ })).toHaveAttribute(
      "aria-checked",
      "true",
    ),
  );
  await flushFetches();
});

// #548: the sidebar list re-sorts by watching the shared config's order, so the Settings
// radio must refresh the config after a successful save — and must NOT on a failed one.
test("a session_list_order save refreshes the shared config (#548)", async () => {
  const refresh = vi.fn();
  render(
    <MemoryRouter initialEntries={["/settings/appearance"]}>
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
  await userEvent.click(await screen.findByRole("radio", { name: /Creation date/ }));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  await flushFetches();
});

test("a failed session_list_order save does NOT refresh the config (#548)", async () => {
  const refresh = vi.fn();
  vi.mocked(api.setPrefs).mockRejectedValueOnce(new Error("nope"));
  render(
    <MemoryRouter initialEntries={["/settings/appearance"]}>
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
  await userEvent.click(await screen.findByRole("radio", { name: /Creation date/ }));
  await waitFor(() =>
    expect(screen.getByRole("radio", { name: /Recent activity/ })).toHaveAttribute(
      "aria-checked",
      "true",
    ),
  );
  expect(refresh).not.toHaveBeenCalled();
  await flushFetches();
});
