import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { OverviewPrefsProvider } from "../app/OverviewPrefsContext";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import type { ThemeId } from "../theme/themes";
import { ThemeCtx } from "../theme/themeStore";
import { AccentCtx } from "../theme/accentStore";
import { Settings } from "./Settings";

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
      projects: vi.fn(),
      scrollbackInfo: vi.fn(),
      clearScrollback: vi.fn(),
    },
  };
});

function renderSettings(theme: ThemeId = "dark", accent = "#ffb000") {
  const setTheme = vi.fn();
  const setAccent = vi.fn();
  render(
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme, setTheme }}>
        <AccentCtx.Provider value={{ accent, setAccent }}>
          <OverviewPrefsProvider>
            <Settings />
          </OverviewPrefsProvider>
        </AccentCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  return { setTheme, setAccent };
}

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
  vi.mocked(api.projects).mockResolvedValue({ projects: [] });
  vi.mocked(api.scrollbackInfo).mockResolvedValue({ bytes: 0, files: 0 });
  vi.mocked(api.clearScrollback).mockResolvedValue({ scope: "all", removed: 0, bytes_freed: 0 });
});

test("renders the themes, the version, and a safe coffee link", async () => {
  renderSettings();
  expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  for (const label of ["Dark", "Light"]) {
    expect(screen.getByRole("radio", { name: new RegExp(label) })).toBeInTheDocument();
  }
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));

  const coffee = screen.getByRole("link", { name: /buy me a coffee/i });
  expect(coffee).toHaveAttribute("href", "https://buymeacoffee.com/teriansilva");
  expect(coffee).toHaveAttribute("target", "_blank");
  expect(coffee).toHaveAttribute("rel", "noopener noreferrer");
});

test("the active theme is marked aria-checked", async () => {
  renderSettings("light");
  expect(screen.getByRole("radio", { name: /Light/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: /Dark/ })).toHaveAttribute("aria-checked", "false");
  // flush the pending version fetch so its state update doesn't warn outside act()
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("picking a theme calls setTheme with its id", async () => {
  const { setTheme } = renderSettings("light");
  await userEvent.click(screen.getByRole("radio", { name: /Dark/ }));
  expect(setTheme).toHaveBeenCalledWith("dark");
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("picking an accent preset calls setAccent with its hex (#211 Phase 2)", async () => {
  const { setAccent } = renderSettings("dark");
  await userEvent.click(screen.getByRole("radio", { name: "Signal Red" }));
  expect(setAccent).toHaveBeenCalledWith("#c02020");
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("the active accent preset is marked aria-checked", async () => {
  renderSettings("dark", "#c02020");
  expect(screen.getByRole("radio", { name: "Signal Red" })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: "Amber" })).toHaveAttribute("aria-checked", "false");
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("committing a custom hex (Enter) calls setAccent normalized", async () => {
  const { setAccent } = renderSettings("dark");
  const field = screen.getByLabelText("Accent hex value");
  await userEvent.clear(field);
  await userEvent.type(field, "#3FBF6F{Enter}");
  expect(setAccent).toHaveBeenCalledWith("#3fbf6f");
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("an invalid custom hex is rejected (no setAccent) and the field resets", async () => {
  const { setAccent } = renderSettings("dark", "#ffb000");
  const field = screen.getByLabelText("Accent hex value");
  await userEvent.clear(field);
  await userEvent.type(field, "zzz{Enter}");
  expect(setAccent).not.toHaveBeenCalled();
  expect(field).toHaveValue("#ffb000"); // reset to the active accent
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("renders the Connected agents section with each engine + new-session badge", async () => {
  renderSettings();
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
  renderSettings();
  expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
  await waitFor(() => expect(screen.getByText("Linux 6.8.0")).toBeInTheDocument());
  // CPU + load, humanized memory (8/16 GB used/total), humanized uptime (90000s = 1d 1h)
  expect(screen.getByText(/8 cores · load 0\.50/)).toBeInTheDocument();
  expect(screen.getByText("8.0 GB / 16 GB")).toBeInTheDocument();
  expect(screen.getByText("1d 1h")).toBeInTheDocument();
});

test("2FA: enable flow shows QR + manual key + recovery codes, then confirms", async () => {
  vi.mocked(api.enroll2fa).mockResolvedValue({
    secret: "JBSWY3DPEHPK3PXP",
    otpauth_uri: "otpauth://totp/BattleLab:marcus?secret=JBSWY3DPEHPK3PXP&issuer=BattleLab",
    recovery_codes: ["aaaa-bbbb-cccc", "dddd-eeee-ffff"],
  });
  vi.mocked(api.confirm2fa).mockResolvedValue(undefined);
  renderSettings();
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
  renderSettings();
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
  expect(screen.queryByRole("heading", { name: /two-factor authentication/i })).toBeNull();
});

test("Updates: check finds an update, then apply calls the API", async () => {
  vi.mocked(api.updateCheck).mockResolvedValue({
    current: "0.0.1",
    channel: "main",
    latest: "abc1234",
    update_available: true,
  });
  vi.mocked(api.updateApply).mockResolvedValue({ status: "updating" });
  renderSettings();
  await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));
  expect(await screen.findByText(/update available: abc1234/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /update now/i }));
  expect(api.updateApply).toHaveBeenCalled();
  expect(await screen.findByText(/will restart/i)).toBeInTheDocument();
});

test("Session overview: unticking a project hides it + persists via projects_hidden (#174)", async () => {
  vi.mocked(api.projects).mockResolvedValue({
    projects: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/beta", label: "Beta" },
    ],
  });
  renderSettings();
  // Inverse semantics (#174): the row starts CHECKED (visible). Unticking hides.
  const alpha = await screen.findByRole("checkbox", { name: /~\/alpha/i });
  expect(alpha).toBeChecked();
  await userEvent.click(alpha);
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_hidden: ["/home/u/alpha"] });
  // Shared state updates → the row immediately reflects the hidden state (now unchecked).
  expect(await screen.findByRole("checkbox", { name: /~\/alpha/i })).not.toBeChecked();
});

test("Session overview: renaming via the modal persists via setProjectName (#174)", async () => {
  vi.mocked(api.projects).mockResolvedValue({
    projects: [{ cwd: "/home/u/alpha", label: "Alpha" }],
  });
  renderSettings();
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
  vi.mocked(api.projects).mockResolvedValue({ projects: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
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
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <ConfigCtx.Provider value={cfg}>
          <OverviewPrefsProvider>
            <Settings />
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
  await waitFor(() => expect(screen.getByText("Saved Alpha")).toBeInTheDocument());
});

test.each([
  ["/s/claude/abc", "/s/claude/abc"], // an in-app session path is honored
  ["/overview", "/overview"], // any internal path is fine
  [undefined, "/"], // opened directly (no state) → landing
  ["//evil.example", "/"], // protocol-relative → rejected
  ["https://evil.example", "/"], // absolute external → rejected
  ["/settings", "/"], // self → rejected (no loop)
])("Settings back link honors only internal returnTo: %s → %s (#155)", (returnTo, expected) => {
  render(
    <MemoryRouter initialEntries={[{ pathname: "/settings", state: returnTo && { returnTo } }]}>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: vi.fn() }}>
        <OverviewPrefsProvider>
          <Settings />
        </OverviewPrefsProvider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  expect(screen.getByRole("link", { name: "Back to sessions" })).toHaveAttribute("href", expected);
});

test("About: the creator name links to superstatus.io", async () => {
  renderSettings();
  const link = await screen.findByRole("link", { name: "Marcus Braun" });
  expect(link).toHaveAttribute("href", "https://superstatus.io");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

test("Account: Sign out calls the logout API (#141)", async () => {
  renderSettings();
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
  renderSettings();
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
  expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull();
});

test("Maintenance: archive-older confirms then calls the API with the chosen hours (#142)", async () => {
  vi.mocked(api.archiveOlder).mockResolvedValue({ archived: 2, skipped: 1 });
  renderSettings();
  // Default age is 168h; first click reveals the confirm step (no API call yet).
  await userEvent.click(await screen.findByRole("button", { name: /archive older/i }));
  expect(api.archiveOlder).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: /confirm archive/i }));
  expect(api.archiveOlder).toHaveBeenCalledWith(168);
  expect(await screen.findByText(/archived 2 sessions \(1 skipped\)\./i)).toBeInTheDocument();
});

test("Maintenance: cancel backs out without archiving (#142)", async () => {
  renderSettings();
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
  renderSettings();
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
  renderSettings();
  await userEvent.click(await screen.findByRole("button", { name: /clear archived sessions/i }));
  await userEvent.click(screen.getByRole("button", { name: /confirm clear archived/i }));
  expect(api.clearScrollback).toHaveBeenCalledWith("archived");
});
