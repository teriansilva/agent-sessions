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

test("welcome → agents lists discovered engines from /api/engines", async () => {
  renderWizard();
  expect(screen.getByText(/welcome to battlelab/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
  expect(screen.getByText("gemini")).toBeInTheDocument();
  expect(screen.getByText(/not found/i)).toBeInTheDocument();
});

test("Skip setup persists onboarded and closes the wizard", async () => {
  const onClose = renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /skip setup/i }));
  expect(api.completeOnboarding).toHaveBeenCalledTimes(1);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("AI step saves via the ai_review prefs contract (key write-only)", async () => {
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
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
  expect(screen.getByText(/your sessions/i)).toBeInTheDocument();
  // 5 slides (incl. Home Free, #662): advance to the last, then Done.
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(screen.getByText(/home free — from anywhere/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /^done$/i }));
  expect(onClose).toHaveBeenCalledTimes(1);
  // The standalone tour never persists onboarding.
  expect(api.completeOnboarding).not.toHaveBeenCalled();
});
