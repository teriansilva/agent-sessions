import { act, render, screen, waitFor, within } from "@testing-library/react";
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
    testAiEndpoint: vi.fn(),
    // #1009: the Usage analytics step saves through this, and reconciles a failure with config().
    setAnalyticsConsent: vi.fn(),
    config: vi.fn(),
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
  vi.mocked(api.engines)
    .mockReset()
    .mockResolvedValue({
      engines: [
        { id: "claude", present: true, supports_new: true, bin: "/x/claude" },
        { id: "gemini", present: false, supports_new: false, bin: null },
      ],
    });
  vi.mocked(api.folders)
    .mockReset()
    .mockResolvedValue({
      folders: [{ cwd: "/home/u/battlelab", label: "battlelab" }],
    });
  vi.mocked(api.fsDirs)
    .mockReset()
    .mockResolvedValue({ path: "/home/u", home: "/home/u", dirs: [] });
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
  vi.mocked(api.aiReviewModels).mockReset().mockResolvedValue({ models: [] });
  vi.mocked(api.completeOnboarding).mockReset().mockResolvedValue({});
  vi.mocked(api.setAnalyticsConsent).mockReset();
  vi.mocked(api.config).mockReset();
  vi.mocked(api.createProject)
    .mockReset()
    .mockResolvedValue({
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
  expect(
    screen.getByRole("heading", { name: /secure your deck/i }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /add two-factor/i }),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
  expect(screen.getByText("gemini")).toBeInTheDocument();
  expect(screen.getByText(/not found/i)).toBeInTheDocument();
});

test("security step: login-off (relay) shows the skip panel + how-to-enable-login, not 2FA", async () => {
  renderWizard(vi.fn(), cfg({ auth_mode: "none" }));
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  expect(screen.getByText(/login is off/i)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /add two-factor/i }),
  ).not.toBeInTheDocument();
  // #682: the login-off step now explains how to enable login, with the verified recipe.
  expect(screen.getByText(/prefer a password login/i)).toBeInTheDocument();
  expect(
    screen.getByText(
      (c, el) =>
        el?.tagName === "CODE" && c.includes("reset-password --prompt"),
    ),
  ).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: /skip — continue/i }),
  );
  expect(await screen.findByText("claude")).toBeInTheDocument();
});

test("security step: a re-run with 2FA already enabled shows it as on, not an enroll offer", async () => {
  // #675 (Hermes): re-enrolling when 2FA is already on needs fresh proof (server 403s), so the
  // replay path must render the enabled state from config instead of offering enrollment.
  renderWizard(vi.fn(), cfg({ two_factor_enabled: true }));
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  expect(
    screen.getByText(/two-factor authentication is on/i),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /add two-factor/i }),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
});

test("Skip setup persists onboarded and closes the wizard", async () => {
  const onClose = renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /skip setup/i }));
  expect(api.completeOnboarding).toHaveBeenCalledTimes(1);
  // Finishing setup covers the current release notes, so the dialog never follows the wizard (#971).
  expect(api.completeOnboarding).toHaveBeenCalledWith("0.20.0");
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
    expect.objectContaining({
      state: { fresh: { cwd: "/home/u/battlelab", bypass: true } },
    }),
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
  await userEvent.click(
    screen.getByRole("button", { name: /launch session/i }),
  );
  expect(mockNavigate).toHaveBeenCalledWith(
    expect.stringMatching(/^\/s\/claude\//),
    expect.objectContaining({
      state: { fresh: { cwd: "/home/u/other", bypass: true } },
    }),
  );
});

// The AI step is the same Endpoint & model component Settings uses (#956), so the wizard cannot
// keep a diverging copy of the old "Save & validate" flow. Its behaviour is pinned in
// AiEndpointSetup.test.tsx; these tests pin that the wizard hosts it and never blocks setup.

const SAVED_OPENAI = {
  enabled: false,
  base_url: "https://api.openai.com/v1",
  model: "",
  interval_minutes: 5,
  max_input_chars: 24000,
  request_timeout: null,
  api_key_set: true,
  configured: true,
};

test("AI step: Save connection checks, then saves, then refreshes config — and Save model stores the model (#692/#956)", async () => {
  const user = userEvent.setup();
  const refresh = vi.fn();
  vi.mocked(api.testAiEndpoint).mockResolvedValue({
    models: ["gpt-4o", "o3-mini"],
    listing: "ok",
  });
  vi.mocked(api.setPrefs).mockImplementation(async (p) => ({
    ai_review: {
      ...SAVED_OPENAI,
      ...(p as { ai_review: { model?: string } }).ai_review,
    },
  }));
  renderWizard(vi.fn(), cfg(), refresh);
  await gotoAiStep();
  expect(
    screen.queryByRole("button", { name: /save & validate/i }),
  ).not.toBeInTheDocument();

  await user.type(screen.getByLabelText(/Base URL/i), "https://api.openai.com/v1");
  await user.type(screen.getByLabelText(/API key/i), "sk-secret");
  await user.click(screen.getByRole("button", { name: "Save connection" }));

  const body = { base_url: "https://api.openai.com/v1", api_key: "sk-secret" };
  expect(api.testAiEndpoint).toHaveBeenCalledWith(body);
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: body }),
  );
  // Settings → AI must reflect the wizard's save without a reload (#692).
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  const combo = await screen.findByRole("combobox", { name: "Model" });
  expect(
    within(combo).getByRole("option", { name: "o3-mini" }),
  ).toBeInTheDocument();
  await user.selectOptions(combo, "o3-mini");
  await user.click(screen.getByRole("button", { name: "Save model" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { model: "o3-mini", request_timeout: null },
    }),
  );
});

test("AI step: a failed check says why and never blocks setup (#692/#956)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.testAiEndpoint).mockRejectedValue(
    new ApiError("401 Unauthorized", 502),
  );
  renderWizard();
  await gotoAiStep();
  await user.type(screen.getByLabelText(/Base URL/i), "https://api.openai.com/v1");
  await user.type(screen.getByLabelText(/API key/i), "sk-bad");
  await user.click(screen.getByRole("button", { name: "Save connection" }));
  expect(
    await screen.findByText("✗ Not saved — 401 Unauthorized"),
  ).toBeInTheDocument();
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /^next$/i })).toBeEnabled();
});

test("AI step: skipping is always possible — nothing configured, Next still works", async () => {
  renderWizard();
  await gotoAiStep();
  expect(screen.getByRole("button", { name: /^next$/i })).toBeEnabled();
  expect(
    screen.getByRole("button", { name: /i'll do this later/i }),
  ).toBeEnabled();
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
  expect(screen.getByText("1 / 10")).toBeInTheDocument();
  // 10 slides (#971 refresh): Mission control, then Files/git/editing and Templates, then the rest.
  const next = () => userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await next();
  expect(screen.getByRole("heading", { name: "Mission control" })).toBeInTheDocument();
  expect(screen.queryByText(/what's in flight/i)).not.toBeInTheDocument();
  await next();
  expect(screen.getByRole("heading", { name: "Files, git & editing" })).toBeInTheDocument();
  await next();
  expect(screen.getByRole("heading", { name: "Templates" })).toBeInTheDocument();
  for (let k = 0; k < 4; k++) await next();
  expect(screen.getByText(/home free — from anywhere/i)).toBeInTheDocument();
  await next();
  await next();
  expect(screen.getByText("10 / 10")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^done$/i }));
  expect(onClose).toHaveBeenCalledTimes(1);
  // The standalone tour never persists onboarding.
  expect(api.completeOnboarding).not.toHaveBeenCalled();
});

test("the tour offers What's new when the shell can open it (#971)", async () => {
  const onWhatsNew = vi.fn();
  render(
    <MemoryRouter>
      <ConfigCtx.Provider value={cfg()}>
        <Onboarding mode="tour" onClose={vi.fn()} onWhatsNew={onWhatsNew} />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
  await userEvent.click(screen.getByRole("button", { name: "What's new in 0.20" }));
  expect(onWhatsNew).toHaveBeenCalledTimes(1);
});

test("the tour answers the arrow keys (#971)", async () => {
  render(
    <MemoryRouter>
      <ConfigCtx.Provider value={cfg()}>
        <Onboarding mode="tour" onClose={vi.fn()} />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByText("2 / 10")).toBeInTheDocument();
  await userEvent.keyboard("{ArrowLeft}");
  expect(screen.getByText("1 / 10")).toBeInTheDocument();
  // Without the shell's callback there is nothing to open, so no button.
  expect(screen.queryByRole("button", { name: /what's new/i })).not.toBeInTheDocument();
});

// ---- Usage analytics (#1009) ----------------------------------------------------------------

const UNDECIDED = { enabled: false, decided: false, available: true };
const ANALYTICS_ON = { enabled: true, decided: true, available: true };

async function gotoAnalyticsStep() {
  await gotoLaunchStep(); // → project
  await finishTourToLaunch(); // Finish tour → the analytics step, when the server offers it
  expect(
    await screen.findByRole("heading", { name: "Usage analytics" }),
  ).toBeInTheDocument();
}

function railLabels() {
  const rail = screen.getByRole("navigation", { name: "Setup steps" });
  return within(rail)
    .getAllByText(/./, { selector: "span:not([class*=stepDot]):not([class*=railHead])" })
    .map((el) => el.textContent?.replace(/^\d+/, "").trim());
}

test("usage analytics: a fresh install is asked between Tour and Launch, unticked; ticking saves true", async () => {
  vi.mocked(api.setAnalyticsConsent).mockResolvedValue({ analytics: ANALYTICS_ON });
  const refresh = vi.fn();
  renderWizard(vi.fn(), cfg({ analytics: UNDECIDED }), refresh);
  const labels = railLabels();
  expect(labels.slice(-3)).toEqual(["Tour", "Usage analytics", "Launch"]);
  await gotoAnalyticsStep();
  const box = screen.getByRole("checkbox", { name: "Share usage analytics" });
  expect(box).not.toBeChecked();
  expect(
    screen.getByText(/Nothing is sent unless you tick the box and continue\./),
  ).toBeInTheDocument();
  await userEvent.click(box);
  await userEvent.click(screen.getByRole("button", { name: /^continue/i }));
  expect(api.setAnalyticsConsent).toHaveBeenCalledWith(true);
  // The refreshed config fetch is what lets the first day count.
  expect(refresh).toHaveBeenCalled();
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
});

test("usage analytics: continuing without ticking records a no", async () => {
  vi.mocked(api.setAnalyticsConsent).mockResolvedValue({
    analytics: { enabled: false, decided: true, available: true },
  });
  renderWizard(vi.fn(), cfg({ analytics: UNDECIDED }));
  await gotoAnalyticsStep();
  await userEvent.click(screen.getByRole("button", { name: /^continue/i }));
  expect(api.setAnalyticsConsent).toHaveBeenCalledWith(false);
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
});

test("usage analytics: the choice and Back are frozen while the save is pending, so a late edit cannot be dropped", async () => {
  let resolveSave: (v: { analytics: typeof ANALYTICS_ON }) => void = () => {};
  vi.mocked(api.setAnalyticsConsent).mockReturnValue(
    new Promise((res) => {
      resolveSave = res;
    }),
  );
  renderWizard(vi.fn(), cfg({ analytics: UNDECIDED }));
  await gotoAnalyticsStep();
  const box = screen.getByRole("checkbox", { name: "Share usage analytics" });
  await userEvent.click(box); // tick
  await userEvent.click(screen.getByRole("button", { name: /^continue/i })); // POST true, pending
  expect(box).toBeDisabled();
  expect(screen.getByRole("button", { name: /back/i })).toBeDisabled();
  await userEvent.click(box); // an untick attempt while pending does nothing
  expect(box).toBeChecked();
  await act(async () => {
    resolveSave({ analytics: ANALYTICS_ON });
  });
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
  expect(api.setAnalyticsConsent).toHaveBeenCalledTimes(1);
  expect(api.setAnalyticsConsent).toHaveBeenCalledWith(true);
});

test("usage analytics: an onboarded operator replaying setup also starts unticked", async () => {
  renderWizard(vi.fn(), cfg({ onboarded: true, analytics: UNDECIDED }));
  await gotoAnalyticsStep();
  expect(
    screen.getByRole("checkbox", { name: "Share usage analytics" }),
  ).not.toBeChecked();
});

test("usage analytics: a stored decision wins, and the closing line says it stays in effect", async () => {
  renderWizard(vi.fn(), cfg({ analytics: ANALYTICS_ON }));
  await gotoAnalyticsStep();
  expect(
    screen.getByRole("checkbox", { name: "Share usage analytics" }),
  ).toBeChecked();
  expect(
    screen.getByText(/Your current setting stays in effect until you continue\./),
  ).toBeInTheDocument();
});

test("usage analytics: the server's kill switch removes the step", async () => {
  renderWizard(
    vi.fn(),
    cfg({ analytics: { enabled: false, decided: false, available: false } }),
  );
  expect(railLabels()).not.toContain("Usage analytics");
  await gotoLaunchStep();
  await finishTourToLaunch();
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
  expect(api.setAnalyticsConsent).not.toHaveBeenCalled();
});

test("usage analytics: a failed untick on an enabled replay stays, names 'on', and offers Try again", async () => {
  vi.mocked(api.setAnalyticsConsent).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(api.config).mockResolvedValue(cfg({ onboarded: true, analytics: ANALYTICS_ON }));
  renderWizard(vi.fn(), cfg({ onboarded: true, analytics: ANALYTICS_ON }));
  await gotoAnalyticsStep();
  const box = screen.getByRole("checkbox", { name: "Share usage analytics" });
  expect(box).toBeChecked();
  await userEvent.click(box);
  await userEvent.click(screen.getByRole("button", { name: /^continue/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "your previous setting (on) is still in effect",
  );
  expect(screen.getByRole("heading", { name: "Usage analytics" })).toBeInTheDocument();
  // Try again, and this time it lands.
  vi.mocked(api.setAnalyticsConsent).mockResolvedValueOnce({
    analytics: { enabled: false, decided: true, available: true },
  });
  await userEvent.click(screen.getByRole("button", { name: /^try again/i }));
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
});

test("usage analytics: a lost response whose write landed advances", async () => {
  vi.mocked(api.setAnalyticsConsent).mockRejectedValueOnce(new Error("timeout"));
  vi.mocked(api.config).mockResolvedValue(cfg({ analytics: ANALYTICS_ON }));
  renderWizard(vi.fn(), cfg({ analytics: UNDECIDED }));
  await gotoAnalyticsStep();
  await userEvent.click(screen.getByRole("checkbox", { name: "Share usage analytics" })); // tick
  await userEvent.click(screen.getByRole("button", { name: /^continue/i }));
  expect(
    await screen.findByRole("heading", { name: /start your first session/i }),
  ).toBeInTheDocument();
});

test("usage analytics: when the save and the read both fail, the setting is reported unknown", async () => {
  vi.mocked(api.setAnalyticsConsent).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(api.config).mockRejectedValueOnce(new Error("offline"));
  renderWizard(vi.fn(), cfg({ analytics: UNDECIDED }));
  await gotoAnalyticsStep();
  await userEvent.click(screen.getByRole("button", { name: /^continue/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "the current setting couldn't be read",
  );
  expect(screen.getByRole("heading", { name: "Usage analytics" })).toBeInTheDocument();
});

test("usage analytics: skipping setup records no analytics decision", async () => {
  const onClose = vi.fn();
  renderWizard(onClose, cfg({ analytics: UNDECIDED }));
  await userEvent.click(screen.getByRole("button", { name: /skip setup/i }));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
  expect(api.completeOnboarding).toHaveBeenCalled();
  expect(api.setAnalyticsConsent).not.toHaveBeenCalled();
});
