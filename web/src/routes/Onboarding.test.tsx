import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
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
    aiReviewModels: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 0) {
      super(message);
      this.status = status;
    }
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

// Welcome → Security → Connected agents → Set up your AI.
async function gotoAiStep() {
  await userEvent.click(screen.getByRole("button", { name: /get started/i })); // → security
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i })); // → agents
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → ai
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

function renderWizard(onClose = vi.fn(), config = cfg(), refresh = vi.fn()) {
  render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={refresh}>
        <ConfigCtx.Provider value={config}>
          <Onboarding mode="wizard" onClose={onClose} />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
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
  vi.mocked(api.aiReviewModels).mockReset().mockResolvedValue({ models: [] });
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

test("AI step: Save & validate persists the endpoint (blank model omitted) then refreshes config (#692)", async () => {
  const refresh = vi.fn();
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://api.openai.com/v1", model: "", configured: true },
  });
  renderWizard(vi.fn(), cfg(), refresh);
  await gotoAiStep();
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://api.openai.com/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-secret");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  // Blank model is NOT sent; the key is (write-only). Then the config context is refreshed
  // so Settings → AI reflects the wizard's save without a page reload (the reported bug).
  expect(api.setPrefs).toHaveBeenCalledWith({
    ai_review: { base_url: "https://api.openai.com/v1", api_key: "sk-secret" },
  });
  await waitFor(() => expect(refresh).toHaveBeenCalled());
});

test("AI step: a blank API key is omitted so a stored key is preserved (#692)", async () => {
  // Re-run with an already-configured endpoint (api_key_set): leaving the key blank must NOT
  // send api_key="" — that would risk clobbering the stored secret.
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://api.openai.com/v1", model: "", configured: true },
  });
  renderWizard(
    vi.fn(),
    cfg({ ai_review: { base_url: "https://api.openai.com/v1", model: "", api_key_set: true, configured: true } } as Partial<AppConfig>),
  );
  await gotoAiStep();
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({
    ai_review: { base_url: "https://api.openai.com/v1" },
  });
});

test("AI step: a successful /models probe turns the Model field into a dropdown (#692)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://api.openai.com/v1", model: "", configured: true },
  });
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: ["gpt-4o", "o3-mini"] });
  const refresh = vi.fn();
  renderWizard(vi.fn(), cfg(), refresh);
  await gotoAiStep();
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://api.openai.com/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-secret");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  // The Model control becomes a combobox populated from the endpoint's /models.
  const combo = await screen.findByRole("combobox", { name: /model/i });
  expect(within(combo).getByRole("option", { name: "gpt-4o" })).toBeInTheDocument();
  expect(within(combo).getByRole("option", { name: "o3-mini" })).toBeInTheDocument();
  expect(screen.getByText(/endpoint validated — 2 models/i)).toBeInTheDocument();
  // Picking a model persists it as its own partial patch + refreshes config.
  await userEvent.selectOptions(combo, "o3-mini");
  expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { model: "o3-mini" } });
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(2));
});

test("AI step: an empty model list keeps free-text entry and never blocks setup (#692)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://ollama.local/v1", model: "", configured: true },
  });
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: [] });
  renderWizard();
  await gotoAiStep();
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://ollama.local/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-x");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(await screen.findByText(/doesn.t list models/i)).toBeInTheDocument();
  // Still a free-text field (no combobox), and Next is available — setup isn't blocked.
  expect(screen.getByRole("textbox", { name: /model/i })).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: /model/i })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^next$/i })).toBeEnabled();
});

test("AI step: a model typed in the free-text fallback persists on blur (#692 / PR #693)", async () => {
  // Hermes-flagged regression: with no model list, a typed model must still be written. The blur
  // handler compares against the PERSISTED model (not the live input, which onChange has synced),
  // so committing the draft actually sends the { ai_review: { model } } patch.
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://ollama.local/v1", model: "", configured: true },
  });
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: [] }); // no list → free-text fallback
  renderWizard();
  await gotoAiStep();
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://ollama.local/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-x");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  const modelInput = await screen.findByRole("textbox", { name: /model/i });
  await userEvent.type(modelInput, "llama-3.1-70b-instruct");
  await userEvent.tab(); // blur commits the draft
  expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { model: "llama-3.1-70b-instruct" } });
});

test("AI step: clearing a seeded free-text model persists the empty value (#692 / PR #693)", async () => {
  // Hermes-flagged follow-up: on a re-run with an existing model, deleting it and blurring must
  // write { model: "" } — not silently keep the old server-side model.
  renderWizard(
    vi.fn(),
    cfg({
      ai_review: {
        base_url: "https://ollama.local/v1",
        model: "llama-3",
        api_key_set: true,
        configured: true,
      },
    } as Partial<AppConfig>),
  );
  await gotoAiStep();
  const modelInput = screen.getByRole("textbox", { name: /model/i });
  expect(modelInput).toHaveValue("llama-3"); // seeded from config
  await userEvent.clear(modelInput);
  await userEvent.tab(); // blur commits the cleared value
  expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { model: "" } });
});

test("AI step: a validation error surfaces the gateway message + keeps free-text (#692)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: { base_url: "https://api.openai.com/v1", model: "", configured: true },
  });
  vi.mocked(api.aiReviewModels).mockRejectedValue(new ApiError("401 Unauthorized", 401));
  renderWizard();
  await gotoAiStep();
  await userEvent.type(screen.getByPlaceholderText(/api\.openai\.com/i), "https://api.openai.com/v1");
  await userEvent.type(screen.getByPlaceholderText(/never echoed/i), "sk-bad");
  await userEvent.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(await screen.findByText(/401 Unauthorized/i)).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: /model/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^next$/i })).toBeEnabled();
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
  expect(screen.getByText(/six engines, one deck/i)).toBeInTheDocument();
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
