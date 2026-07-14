import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { Onboarding } from "./Onboarding";

vi.mock("../lib/api", () => ({
  api: {
    engines: vi.fn(),
    folders: vi.fn(),
    fsDirs: vi.fn(),
    fsMkdir: vi.fn(),
    setPrefs: vi.fn(),
    completeOnboarding: vi.fn(),
    createProject: vi.fn(),
  },
}));

// #681: capture the launch navigation so we can assert the folder actually launched with.
const { mockNavigate } = vi.hoisted(() => ({ mockNavigate: vi.fn() }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

// Walk the wizard from Welcome to the Launch step (single-user cfg; one new-session engine).
async function gotoLaunchStep() {
  await userEvent.click(screen.getByRole("button", { name: /get started/i })); // → security
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i })); // → agents
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → ai
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → project
}
async function finishTourToLaunch() {
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → tour
  for (let k = 0; k < 12; k++) {
    const next = screen.queryByRole("button", { name: /^next$/i });
    if (!next) break;
    await userEvent.click(next);
  }
  await userEvent.click(screen.getByRole("button", { name: /finish tour/i })); // → launch
}

function cfg(over: Partial<AppConfig> = {}): AppConfig {
  return {
    csrf: "t",
    new_session_engines: ["claude"],
    terminal_backend: "ws",
    onboarded: false,
    ...over,
  } as AppConfig;
}

function renderWizard(onClose = vi.fn(), config = cfg()) {
  render(
    <MemoryRouter>
      <ConfigCtx.Provider value={config}>
        <Onboarding mode="wizard" onClose={onClose} />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
  return onClose;
}

beforeEach(() => {
  mockNavigate.mockReset();
  vi.mocked(api.engines).mockReset().mockResolvedValue({
    engines: [
      { id: "claude", present: true, supports_new: true, bin: "/x/claude" },
      { id: "gemini", present: false, supports_new: false, bin: null },
    ],
  });
  vi.mocked(api.folders).mockReset().mockResolvedValue({
    folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }],
  });
  vi.mocked(api.fsDirs).mockReset().mockResolvedValue({ path: "/home/u", home: "/home/u", dirs: [] });
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
  vi.mocked(api.completeOnboarding).mockReset().mockResolvedValue({});
  vi.mocked(api.createProject).mockReset().mockResolvedValue({
    id: "p1",
    name: "BattleLab Ops",
    color: "",
    folders: ["/home/u/battlelab"],
    default_folder: "/home/u/battlelab",
    archived: false,
    created_at: 0,
  });
});

test("welcome → security → agents lists discovered engines from /api/engines", async () => {
  renderWizard();
  expect(screen.getByText(/welcome to battlelab/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  // #675: the new Security step sits between Welcome and Connected agents.
  expect(screen.getByRole("heading", { name: /secure your deck/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /add two-factor/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
  expect(screen.getByText("gemini")).toBeInTheDocument();
  expect(screen.getByText(/not found/i)).toBeInTheDocument();
});

test("security step: login-off (relay) shows the skip panel + how-to-enable-login, not 2FA", async () => {
  renderWizard(vi.fn(), cfg({ auth_mode: "none" }));
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  expect(screen.getByText(/login is off/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /add two-factor/i })).not.toBeInTheDocument();
  // #682: the login-off step now explains how to enable login, with the verified recipe.
  expect(screen.getByText(/prefer a password login/i)).toBeInTheDocument();
  expect(
    screen.getByText((c, el) => el?.tagName === "CODE" && c.includes("reset-password --prompt")),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /skip — continue/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
});

test("security step: a re-run with 2FA already enabled shows it as on, not an enroll offer", async () => {
  // #675 (Hermes): re-enrolling when 2FA is already on needs fresh proof (server 403s), so the
  // replay path must render the enabled state from config instead of offering enrollment.
  renderWizard(vi.fn(), cfg({ two_factor_enabled: true }));
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  expect(screen.getByText(/two-factor authentication is on/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /add two-factor/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
});

test("Skip setup persists onboarded and closes the wizard", async () => {
  const onClose = renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /skip setup/i }));
  expect(api.completeOnboarding).toHaveBeenCalledTimes(1);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("Launch is enabled and launches with the discovered folder as the fallback (no default_project) — #681", async () => {
  // Regression: cwd stayed "" (no default_project) while the folder <select> visibly showed the
  // first folder, so the `!cwd` guard kept "Launch session" disabled. The effective folder now
  // falls back to the first discovered one, so the button is enabled and launch() uses it.
  renderWizard();
  await gotoLaunchStep();
  // The fallback is already reflected in the project step's Launch-folder select.
  expect(screen.getByRole("combobox")).toHaveValue("/home/u/battlelab");
  await finishTourToLaunch();
  const launchBtn = screen.getByRole("button", { name: /launch session/i });
  expect(launchBtn).toBeEnabled();
  await userEvent.click(launchBtn);
  expect(mockNavigate).toHaveBeenCalledWith(
    expect.stringMatching(/^\/s\/claude\//),
    expect.objectContaining({ state: { fresh: { cwd: "/home/u/battlelab", bypass: true } } }),
  );
});

test("an explicitly chosen folder wins over the discovered-folder fallback — #681", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/battlelab", label: "battlelab" },
      { cwd: "/home/u/other", label: "other" },
    ],
  });
  renderWizard();
  await gotoLaunchStep();
  // Pick the second folder in the project step's Launch-folder select; it must survive to launch.
  await userEvent.selectOptions(screen.getByRole("combobox"), "/home/u/other");
  await finishTourToLaunch();
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  expect(mockNavigate).toHaveBeenCalledWith(
    expect.stringMatching(/^\/s\/claude\//),
    expect.objectContaining({ state: { fresh: { cwd: "/home/u/other", bypass: true } } }),
  );
});

test("AI step saves via the ai_review prefs contract (key write-only)", async () => {
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i })); // through Security
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  // AI step.
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://api.openai.com/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-secret");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({
    ai_review: { base_url: "https://api.openai.com/v1", api_key: "sk-secret", model: "" },
  });
});

test("tour mode shows the slideshow and Done closes it", async () => {
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <ConfigCtx.Provider value={cfg()}>
        <Onboarding mode="tour" onClose={onClose} />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
  expect(screen.getByText(/five engines, one deck/i)).toBeInTheDocument();
  // 8 slides (#675 refresh): advance to the last, then Done.
  for (let k = 0; k < 5; k++) {
    await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  }
  expect(screen.getByText(/home free — from anywhere/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /^done$/i }));
  expect(onClose).toHaveBeenCalledTimes(1);
  // The standalone tour never persists onboarding.
  expect(api.completeOnboarding).not.toHaveBeenCalled();
});
