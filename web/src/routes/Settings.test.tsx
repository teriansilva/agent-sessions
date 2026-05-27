import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { ThemeId } from "../theme/themes";
import { ThemeCtx } from "../theme/themeStore";
import { Settings } from "./Settings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      version: vi.fn(),
      setTheme: vi.fn(),
      engines: vi.fn(),
      system: vi.fn(),
      updateCheck: vi.fn(),
      updateApply: vi.fn(),
      config: vi.fn(),
      enroll2fa: vi.fn(),
      confirm2fa: vi.fn(),
      disable2fa: vi.fn(),
      regenerate2fa: vi.fn(),
    },
  };
});

function renderSettings(theme: ThemeId = "royal") {
  const setTheme = vi.fn();
  render(
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme, setTheme }}>
        <Settings />
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
  return { setTheme };
}

beforeEach(() => {
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
});

test("renders the three themes, the version, and a safe coffee link", async () => {
  renderSettings();
  expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
  for (const label of ["Royal", "Dark", "Light"]) {
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
  expect(screen.getByRole("radio", { name: /Royal/ })).toHaveAttribute("aria-checked", "false");
  // flush the pending version fetch so its state update doesn't warn outside act()
  await waitFor(() => expect(screen.getAllByText("1.2.3").length).toBeGreaterThan(0));
});

test("picking a theme calls setTheme with its id", async () => {
  const { setTheme } = renderSettings("royal");
  await userEvent.click(screen.getByRole("radio", { name: /Dark/ }));
  expect(setTheme).toHaveBeenCalledWith("dark");
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
    otpauth_uri: "otpauth://totp/TermRoyale:marcus?secret=JBSWY3DPEHPK3PXP&issuer=TermRoyale",
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
