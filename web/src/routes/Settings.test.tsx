import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
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
  ["projects", ["Projects", "Session overview", "Default project"]],
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

// VT scrollback promoted out of "Experimental" into Appearance (#357 Phase 2) — the
// Experimental section was VT-only and is gone; the toggle's API path is unchanged.
test("VT scrollback lives in Appearance and the Experimental section is gone (#357)", async () => {
  renderSettings("dark", "#ffb000", "/settings/appearance");
  expect(screen.queryByRole("heading", { name: "Experimental" })).not.toBeInTheDocument();
  // The toggle renders inside the Appearance tabpanel with its own subhead.
  const panel = screen.getByRole("tabpanel");
  expect(panel).toHaveAttribute("id", "settings-panel-appearance");
  expect(screen.getByRole("heading", { name: /faithful scroll-up \(vt\)/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: /disabled/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ vt_scrollback: true });
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

test("Default project: lists the pickable (visible) projects and persists a choice", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/beta", label: "Beta" },
    ],
  });
  renderProjectsTab();
  const select = await screen.findByRole("combobox", { name: "Default project" });
  await waitFor(() => expect(select).toBeEnabled());
  // The picker mirrors the new-session picker's pickable set (#335): visible projects only.
  expect(api.folders).toHaveBeenCalledWith({ visible: true });
  expect(select).toHaveValue(""); // no default stored → the "no default" option
  await userEvent.selectOptions(select, "/home/u/beta");
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project: "/home/u/beta" });
  expect(select).toHaveValue("/home/u/beta");
});

test("Default project: seeds from config.default_project and '' clears it", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  renderProjectsTab({ default_project: "/home/u/alpha" });
  const select = await screen.findByRole("combobox", { name: "Default project" });
  await waitFor(() => expect(select).toHaveValue("/home/u/alpha"));
  await userEvent.selectOptions(select, "");
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project: "" });
  expect(select).toHaveValue("");
});

test("Default project: a stale stored default stays visible (and clearable), never hidden", async () => {
  // The new-session picker silently falls back for a gone dir; the Settings control instead
  // SHOWS the stored value so the user can see + clear it.
  vi.mocked(api.folders).mockResolvedValue({ folders: [] });
  renderProjectsTab({ default_project: "/home/u/gone" });
  const select = await screen.findByRole("combobox", { name: "Default project" });
  await waitFor(() => expect(select).toHaveValue("/home/u/gone"));
  expect(screen.getByRole("option", { name: /not currently active/i })).toBeInTheDocument();
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
