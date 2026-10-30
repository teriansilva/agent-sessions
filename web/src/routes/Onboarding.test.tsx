import { act, render, screen, waitFor, within } from "@testing-library/react";
import type { ResourceSettings } from "../types/resources";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AppConfig, EngineInfo } from "../types/api";
import { resetHqChoiceStore } from "../components/hq/hqChoiceStore";
import { Onboarding } from "./Onboarding";

vi.mock("../lib/api", () => ({
  api: {
    resources: vi.fn(() => Promise.reject(new Error("not this test's business"))),
    setResources: vi.fn(),
    engines: vi.fn(),
    folders: vi.fn(),
    fsDirs: vi.fn(),
    fsMkdir: vi.fn(),
    setPrefs: vi.fn(),
    completeOnboarding: vi.fn(),
    createProject: vi.fn(),
    aiReviewModels: vi.fn(),
    testAiEndpoint: vi.fn(),
    // #1657: the provider table the Endpoint & model picker renders (self-hosted presets included).
    aiProviders: vi.fn(async () => ({
      providers: [
        { id: "custom", label: "Custom (OpenAI-compatible)", group: "custom", kind: "openai-chat", base_url: "", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
        { id: "ollama", label: "Ollama", group: "self-hosted", kind: "openai-chat", base_url: "http://localhost:11434/v1", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
        { id: "lmstudio", label: "LM Studio", group: "self-hosted", kind: "openai-chat", base_url: "http://localhost:1234/v1", url_editable: true, vendor: "", thinking_supported: true, auto_model: "", key_hint: "" },
      ],
    })),
    // #1009: the Usage analytics step saves through this, and reconciles a failure with config().
    setAnalyticsConsent: vi.fn(),
    config: vi.fn(),
    // #1739: the Agents step reads the catalog; the Launch step creates an API session.
    plugins: vi.fn(),
    structuredCreate: vi.fn(),
    structuredStart: vi.fn(),
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
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → your system
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
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → your system
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
  resetHqChoiceStore();
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
  vi.mocked(api.plugins)
    .mockReset()
    .mockResolvedValue({
      feed: { state: "ready", error: null },
      catalog: [],
      plugins: [],
      operations: [],
      roster_generation: 1,
      roster_revision: null,
    });
  vi.mocked(api.structuredCreate).mockReset();
  vi.mocked(api.structuredStart).mockReset().mockResolvedValue({} as never);
  sessionStorage.clear();
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

test("welcome → security → agents lists the roster's agents and the catalog's, each with its client (#1739)", async () => {
  vi.mocked(api.plugins).mockResolvedValue({
    feed: { state: "ready", error: null },
    catalog: [
      {
        manifest: {
          identity: { id: "newcomer", label: "Newcomer CLI", publisher: "Example", version: "1.0.0" },
          runtime: { kind: "pty" },
        },
        digest: "d",
      },
      {
        manifest: {
          identity: { id: "blocked", label: "Blocked CLI", publisher: "Example", version: "2.0.0" },
          runtime: { kind: "pty" },
        },
        digest: "e",
        installable: false,
        reason: "not offered for this platform",
      },
      {
        manifest: {
          identity: { id: "lone-api", label: "Lone API", publisher: "Example", version: "1.0.0" },
          runtime: { kind: "api" },
          api: { kind: "x", source: "nowhere" },
        },
        digest: "f",
      },
    ],
    plugins: [],
    operations: [],
    roster_generation: 1,
    roster_revision: null,
  });
  renderWizard();
  expect(
    screen.getByRole("heading", { name: /welcome, commander/i }),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  // #675: the new Security step sits between Welcome and Agents.
  expect(
    screen.getByRole("heading", { name: /secure your deck/i }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /add two-factor/i }),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
  const list = screen.getByRole("list", { name: "Agents" });
  // claude's API client is in the roster but not ready: "API available", with the reason.
  const claude = within(list).getByText("claude").closest("li")!;
  expect(within(claude).getByText("API available")).toBeInTheDocument();
  expect(claude).toHaveTextContent(/2\.1\.287 or later is required/);
  // An agent with no API client at all.
  const gemini = within(list).getByText("gemini").closest("li")!;
  expect(within(gemini).getByText("Terminal only")).toBeInTheDocument();
  // A catalog agent this host lacks: Not installed, with an inline Install.
  const newcomer = within(list).getByText("Newcomer CLI").closest("li")!;
  expect(newcomer).toHaveTextContent(/not installed/i);
  // Every catalog agent is a row (Hermes 7251): one this host can't install says why, no Install…
  const blocked = within(list).getByText("Blocked CLI").closest("li")!;
  expect(blocked).toHaveTextContent(/unavailable/i);
  expect(blocked).toHaveTextContent("not offered for this platform");
  expect(within(blocked).queryByRole("button")).not.toBeInTheDocument();
  // …and an API client whose console agent is nowhere is listed on its own.
  expect(within(list).getByText("Lone API")).toBeInTheDocument();
  await userEvent.click(within(newcomer).getByRole("button", { name: /install/i }));
  // The Add-agent flow opens INLINE, fixed on that agent — setup is not left.
  const flow = await screen.findByRole("region", { name: /set up newcomer cli/i });
  expect(within(flow).getByRole("button", { name: /review installation/i })).toBeInTheDocument();
  expect(within(flow).queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /your agents/i })).toBeInTheDocument();
  expect(mockNavigate).not.toHaveBeenCalled();
});

test("a catalog refresh that fails after a good read withdraws every install action (#1739)", async () => {
  const good = {
    feed: { state: "ready" as const, error: null },
    catalog: [
      {
        manifest: {
          identity: { id: "newcomer", label: "Newcomer CLI", publisher: "Example", version: "1.0.0" },
          runtime: { kind: "pty" as const },
        },
        digest: "d",
      },
    ],
    plugins: [],
    operations: [],
    roster_generation: 1,
    roster_revision: null,
  };
  vi.mocked(api.plugins)
    .mockResolvedValueOnce(good) // the wizard opens
    .mockResolvedValueOnce(good) // the Agents step, first visit
    .mockRejectedValue(new Error("feed unreachable")); // every later read
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByRole("button", { name: /install: newcomer cli/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i })); // → ai
  await userEvent.click(screen.getByRole("button", { name: /^back$/i })); // → agents: re-read fails
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load the agent catalog/i);
  expect(screen.getByText("Newcomer CLI")).toBeInTheDocument(); // the row stays…
  expect(screen.queryByRole("button", { name: /install/i })).not.toBeInTheDocument(); // …inert
});

test("an older catalog read answering after a newer failed one changes nothing (#1739)", async () => {
  let older!: (v: unknown) => void;
  vi.mocked(api.plugins)
    .mockReturnValueOnce(new Promise((r) => (older = r)) as never) // the wizard opens: held
    .mockRejectedValue(new Error("feed unreachable")); // the Agents step's newer read fails
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn’t load the agent catalog/i);
  await act(async () =>
    older({
      feed: { state: "ready", error: null },
      catalog: [
        {
          manifest: {
            identity: { id: "newcomer", label: "Newcomer CLI", publisher: "Example", version: "1.0.0" },
            runtime: { kind: "pty" },
          },
          digest: "d",
        },
      ],
      plugins: [],
      operations: [],
      roster_generation: 1,
      roster_revision: null,
    }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(/couldn’t load the agent catalog/i);
  expect(screen.queryByText("Newcomer CLI")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /install/i })).not.toBeInTheDocument();
});

test("the Agents step says when the catalog couldn't load and still lists the host's agents (#1739)", async () => {
  vi.mocked(api.plugins).mockRejectedValue(new Error("feed unreachable"));
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  expect(await screen.findByText("claude")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/couldn’t load the agent catalog/i);
  expect(screen.queryByRole("button", { name: /^install/i })).not.toBeInTheDocument();
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

test("with the SHARED roster unavailable, Launch is disabled and says why — never an enabled no-op (#853 P4)", async () => {
  // The wizard's own engine discovery succeeds; the roster that decides the id mode has not
  // loaded. An enabled Launch would click through to nothing (Hermes on #1134).
  const { markRosterFailed, resetRoster, setRoster, getRoster } = await import(
    "../app/engineRoster"
  );
  const loaded = getRoster().engines;
  renderWizard();
  await gotoLaunchStep();
  await finishTourToLaunch();
  resetRoster();
  markRosterFailed();
  const launchBtn = await screen.findByRole("button", { name: /launch session/i });
  await waitFor(() => expect(launchBtn).toBeDisabled());
  expect(screen.getByRole("status")).toHaveTextContent(/Couldn’t load the agent list/);
  await userEvent.click(launchBtn);
  expect(mockNavigate).not.toHaveBeenCalled();
  expect(api.completeOnboarding).not.toHaveBeenCalled();
  // …and it becomes available the moment the roster lands.
  act(() => setRoster(loaded));
  await waitFor(() => expect(launchBtn).toBeEnabled());
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
  // A remote endpoint waits for the egress acknowledgement (#1671).
  await user.click(screen.getByRole("checkbox", { name: /I understand session content is sent to/ }));
  await user.click(screen.getByRole("button", { name: "Save connection" }));

  const body = {
    base_url: "https://api.openai.com/v1",
    api_key: "sk-secret",
    egress_acknowledged: true,
  };
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
  // A remote endpoint waits for the egress acknowledgement (#1671).
  await user.click(screen.getByRole("checkbox", { name: /I understand session content is sent to/ }));
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
  expect(
    screen.getByRole("heading", { name: "Welcome, Commander" }),
  ).toBeInTheDocument();
  expect(screen.getByText("1 / 10")).toBeInTheDocument();
  // 10 slides (#1544 refresh): Dashboard, HQ, sessions, Missions (beta), Library, then the rest.
  const next = () => userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await next();
  expect(screen.getByRole("heading", { name: /^HQ/ })).toBeInTheDocument();
  await next();
  expect(screen.getByRole("heading", { name: "Every agent, one deck" })).toBeInTheDocument();
  await next();
  expect(screen.getByRole("heading", { name: "Missions (beta)" })).toBeInTheDocument();
  await next();
  expect(screen.getByRole("heading", { name: "The Library" })).toBeInTheDocument();
  for (let k = 0; k < 4; k++) await next();
  expect(screen.getByText(/home free — from anywhere/i)).toBeInTheDocument();
  await next();
  // The last slide points at where the tour lives now — the operator menu, not a `?` button.
  expect(screen.getByText(/operator menu/i)).toBeInTheDocument();
  expect(screen.queryByText(/the \? in the top bar/i)).not.toBeInTheDocument();
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

// ---- #1739: API first — a ready API client is preselected and its session is created ----

/** The fixture roster with `id`'s API client READY (present, can start). */
async function withReadyApi(id: string) {
  const { setRoster } = await import("../app/engineRoster");
  const fixture = (await import("../test/roster.fixture.json")).default;
  setRoster(
    (fixture.engines as EngineInfo[]).map((e) =>
      e.id === id
        ? { ...e, present: true, supports_new: true, api: { ...e.api!, unavailable_reason: null } }
        : e,
    ),
  );
}

test("Launch preselects a ready API client and creates its session before setup completes (#1739)", async () => {
  await withReadyApi("codex-api");
  vi.mocked(api.structuredCreate).mockResolvedValue({ session_key: "codex-api:made-1" } as never);
  const onClose = renderWizard(vi.fn(), cfg({ new_session_engines: ["claude", "codex-api"] }));
  await gotoLaunchStep();
  await finishTourToLaunch();
  const picker = screen.getByRole("radiogroup", { name: "Agent" });
  expect(within(picker).getByRole("radio", { name: /codex — api/i })).toBeChecked();
  // The terminal client stays one tap away; an API client that can't start says why.
  expect(within(picker).getByRole("radio", { name: /claude code/i })).not.toBeChecked();
  expect(within(picker).getAllByTestId("setup-launch-unready")[0]).toHaveTextContent(/not ready:/i);
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/s/codex-api/made-1"));
  const [engine, cwd, operation] = vi.mocked(api.structuredCreate).mock.calls[0];
  expect([engine, cwd]).toEqual(["codex-api", "/home/u/battlelab"]);
  expect(typeof operation).toBe("string");
  expect(api.completeOnboarding).toHaveBeenCalled();
  expect(onClose).toHaveBeenCalled();
  // No stored skip default in the config: an API client FAILS CLOSED, as in New session — a guarded
  // create, nothing started behind the operator's back (#1339/#1473), and no manual untick needed.
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(false);
  expect(api.structuredStart).not.toHaveBeenCalled();
});

test("an API client skips prompts only when the loaded config's stored default says so (#1739)", async () => {
  await withReadyApi("codex-api");
  vi.mocked(api.structuredCreate).mockResolvedValue({ session_key: "codex-api:made-3" } as never);
  renderWizard(
    vi.fn(),
    cfg({ new_session_engines: ["codex-api"], agent_defaults: { bypass: true } } as Partial<AppConfig>),
  );
  await gotoLaunchStep();
  await finishTourToLaunch();
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).toBeChecked();
  // Pre-ticked by the stored default, so the consequence is said before Launch — as New session
  // does — and the client's line no longer promises approvals (Hermes 7265).
  expect(screen.getByTestId("setup-api-bypass-warning")).toHaveTextContent(/without its sandbox/);
  expect(screen.getByRole("radio", { name: /codex — api/i }).closest("label")).toHaveTextContent(
    /no approval prompts, no sandbox/,
  );
  await userEvent.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  expect(screen.queryByTestId("setup-api-bypass-warning")).not.toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /codex — api/i }).closest("label")).toHaveTextContent(
    /approvals in the pane/,
  );
  await userEvent.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/s/codex-api/made-3"));
  // A skip create: two-phase, started only once the create answered, under its key (#1339).
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(true);
  expect(api.structuredStart).toHaveBeenCalledWith("codex-api:made-3");
});

test("a create that answers after setup was closed opens nothing, starts nothing and completes nothing more (#1739)", async () => {
  await withReadyApi("codex-api");
  let answer!: (v: unknown) => void;
  vi.mocked(api.structuredCreate).mockReturnValue(new Promise((r) => (answer = r)) as never);
  // A stored skip default: the create is a skip create, which must never start once dismissed.
  const onClose = renderWizard(
    vi.fn(),
    cfg({ new_session_engines: ["codex-api"], agent_defaults: { bypass: true } } as Partial<AppConfig>),
  );
  await gotoLaunchStep();
  await finishTourToLaunch();
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  // While it runs, the footer's own ways out are held…
  expect(screen.getByRole("button", { name: /finish without launching/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /back/i })).toBeDisabled();
  // …and Esc (or ✕) closes setup.
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  await act(async () => answer({ session_key: "codex-api:late" }));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(true);
  expect(api.structuredStart).not.toHaveBeenCalled(); // created, left unstarted in the list
  expect(mockNavigate).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(api.completeOnboarding).toHaveBeenCalledTimes(1); // the close's own, nothing after
});

test("closing setup while it is being completed after the create starts and opens nothing (#1739)", async () => {
  await withReadyApi("codex-api");
  vi.mocked(api.structuredCreate).mockResolvedValue({ session_key: "codex-api:made-4" } as never);
  let completed!: (v: unknown) => void;
  vi.mocked(api.completeOnboarding)
    .mockReturnValueOnce(new Promise((r) => (completed = r)) as never)
    .mockResolvedValue({});
  const onClose = renderWizard(
    vi.fn(),
    cfg({ new_session_engines: ["codex-api"], agent_defaults: { bypass: true } } as Partial<AppConfig>),
  );
  await gotoLaunchStep();
  await finishTourToLaunch();
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  await waitFor(() => expect(api.completeOnboarding).toHaveBeenCalledTimes(1)); // the launch's
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  await act(async () => completed({}));
  expect(api.structuredStart).not.toHaveBeenCalled();
  expect(mockNavigate).not.toHaveBeenCalled();
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a refused API create keeps setup open with the server's reason — setup is not completed (#1739)", async () => {
  await withReadyApi("codex-api");
  vi.mocked(api.structuredCreate).mockRejectedValue(new ApiError("codex is signed out", 409));
  const onClose = renderWizard(vi.fn(), cfg({ new_session_engines: ["claude", "codex-api"] }));
  await gotoLaunchStep();
  await finishTourToLaunch();
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  expect(await screen.findByTestId("setup-launch-error")).toHaveTextContent("codex is signed out");
  expect(api.completeOnboarding).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(mockNavigate).not.toHaveBeenCalled();
  // Terminal is one tap away, and launches as before.
  await userEvent.click(screen.getByRole("radio", { name: /claude code/i }));
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  expect(mockNavigate).toHaveBeenCalledWith(expect.stringMatching(/^\/s\/claude\//), expect.anything());
});

test("a lost API create answer keeps its operation id, so the retry is the same create (#1739)", async () => {
  await withReadyApi("codex-api");
  vi.mocked(api.structuredCreate)
    .mockRejectedValueOnce(new ApiError("Bad gateway", 502))
    .mockResolvedValueOnce({ session_key: "codex-api:made-2" } as never);
  renderWizard(vi.fn(), cfg({ new_session_engines: ["codex-api"], agent_defaults: { bypass: false } } as Partial<AppConfig>));
  await gotoLaunchStep();
  await finishTourToLaunch();
  // Guarded: the stored default is off, so the box starts unticked.
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  await screen.findByTestId("setup-launch-error");
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/s/codex-api/made-2"));
  const ids = vi.mocked(api.structuredCreate).mock.calls.map((c) => c[2]);
  expect(ids[0]).toBe(ids[1]);
});

test("with no ready API client, the terminal client is preselected and unready API clients are named (#1739)", async () => {
  renderWizard(vi.fn(), cfg({ new_session_engines: ["codex-api", "claude"] }));
  await gotoLaunchStep();
  await finishTourToLaunch();
  expect(screen.getByRole("radio", { name: /claude code/i })).toBeChecked();
  expect(screen.getByRole("radiogroup", { name: "Agent" })).toHaveTextContent(
    /codex 0\.159\.3 or later is required/,
  );
  await userEvent.click(screen.getByRole("button", { name: /launch session/i }));
  expect(api.structuredCreate).not.toHaveBeenCalled();
  expect(mockNavigate).toHaveBeenCalledWith(expect.stringMatching(/^\/s\/claude\//), expect.anything());
});

// ---- #1544: the HQ contact step follows the AI endpoint; local presets fill the URL ----

const AI_ON = {
  enabled: false,
  base_url: "http://localhost:11434/v1",
  model: "qwen3",
  interval_minutes: 5,
  max_input_chars: 24000,
  request_timeout: null,
  api_key_set: true,
  configured: true,
};

function rail() {
  return screen.getByRole("navigation", { name: /setup steps/i });
}

test("without an AI endpoint there is no HQ contact step: AI → Next lands on the project", async () => {
  renderWizard();
  expect(within(rail()).queryByText("HQ contact")).toBeNull();
  await gotoAiStep();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(
    screen.getByRole("heading", { name: /create your first project/i }),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /back/i }));
  expect(
    screen.getByRole("heading", { name: /set up your ai/i }),
  ).toBeInTheDocument();
});

test("with an AI endpoint the contact step follows AI, and a pick is written to hq_contact", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({ hq_contact_rev: 1 });
  renderWizard(vi.fn(), cfg({ ai_review: AI_ON, hq_contact: "random" }));
  expect(within(rail()).getByText("HQ contact")).toBeInTheDocument();
  await gotoAiStep();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(
    screen.getByRole("heading", { name: /choose your hq contact/i }),
  ).toBeInTheDocument();
  const gallery = screen.getByRole("radiogroup", {
    name: /select your primary hq contact/i,
  });
  expect(
    within(gallery).getByRole("radio", { name: /random/i }),
  ).toHaveAttribute("aria-checked", "true");
  // Anchored: the Random card reads "Today: <name>", so on a day whose contact is Harlan an
  // unanchored match finds that card too (date-dependent).
  await userEvent.click(
    within(gallery).getByRole("radio", { name: /^general jack harlan/i }),
  );
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ hq_contact: "us-man" }),
  );
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(
    screen.getByRole("heading", { name: /create your first project/i }),
  ).toBeInTheDocument();
  // Back from the project returns to the contact step, not past it.
  await userEvent.click(screen.getByRole("button", { name: /back/i }));
  expect(
    screen.getByRole("heading", { name: /choose your hq contact/i }),
  ).toBeInTheDocument();
});

test("Decide later skips the contact step without writing a choice", async () => {
  renderWizard(vi.fn(), cfg({ ai_review: AI_ON }));
  await gotoAiStep();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await userEvent.click(screen.getByRole("button", { name: /decide later/i }));
  expect(
    screen.getByRole("heading", { name: /create your first project/i }),
  ).toBeInTheDocument();
  expect(api.setPrefs).not.toHaveBeenCalledWith(
    expect.objectContaining({ hq_contact: expect.anything() }),
  );
});

test("the contact step appears as soon as the AI save lands in the config", async () => {
  const tree = (config: AppConfig) => (
    <MemoryRouter>
      <ConfigCtx.Provider value={config}>
        <Onboarding mode="wizard" onClose={vi.fn()} />
      </ConfigCtx.Provider>
    </MemoryRouter>
  );
  const { rerender } = render(tree(cfg()));
  await gotoAiStep();
  expect(within(rail()).queryByText("HQ contact")).toBeNull();
  rerender(tree(cfg({ ai_review: AI_ON })));
  expect(within(rail()).getByText("HQ contact")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(
    screen.getByRole("heading", { name: /choose your hq contact/i }),
  ).toBeInTheDocument();
});

test("a local preset fills the Base URL draft and saves nothing", async () => {
  vi.mocked(api.testAiEndpoint).mockClear();
  renderWizard();
  await gotoAiStep();
  // Presets are the self-hosted tiles of the provider picker since #1671.
  const presets = await screen.findByRole("radiogroup", { name: /self-hosted/i });
  await userEvent.click(await within(presets).findByRole("radio", { name: "Ollama" }));
  expect(screen.getByLabelText(/base url/i)).toHaveValue(
    "http://localhost:11434/v1",
  );
  expect(
    within(presets).getByRole("radio", { name: "Ollama" }),
  ).toHaveAttribute("aria-checked", "true");
  await userEvent.click(within(presets).getByRole("radio", { name: "LM Studio" }));
  expect(screen.getByLabelText(/base url/i)).toHaveValue(
    "http://localhost:1234/v1",
  );
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(api.testAiEndpoint).not.toHaveBeenCalled();
});

test("an endpoint removed while the contact step is open moves on, and the stored choice is untouched", async () => {
  const tree = (config: AppConfig) => (
    <MemoryRouter>
      <ConfigCtx.Provider value={config}>
        <Onboarding mode="wizard" onClose={vi.fn()} />
      </ConfigCtx.Provider>
    </MemoryRouter>
  );
  const { rerender } = render(tree(cfg({ ai_review: AI_ON, hq_contact: "europe-woman" })));
  await gotoAiStep();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(
    screen.getByRole("heading", { name: /choose your hq contact/i }),
  ).toBeInTheDocument();
  rerender(
    tree(
      cfg({
        ai_review: { ...AI_ON, api_key_set: false, configured: false },
        hq_contact: "europe-woman",
      }),
    ),
  );
  expect(
    await screen.findByRole("heading", { name: /create your first project/i }),
  ).toBeInTheDocument();
  expect(within(rail()).queryByText("HQ contact")).toBeNull();
  expect(api.setPrefs).not.toHaveBeenCalledWith(
    expect.objectContaining({ hq_contact: expect.anything() }),
  );
});

// ---- "Your system" (#1819) --------------------------------------------------------------------

const systemSettings = (over: Partial<ResourceSettings> = {}) =>
  ({
    values: { max_sessions: 28 },
    recommended: { max_sessions: 28 },
    bounds: { max_sessions: { min: 1, max: 256 } },
    sources: { max_sessions: "recommended" },
    capacity: {
      memory_bytes: 32 * 1024 ** 3,
      cpus: 8,
      reserved_bytes: 3.2 * 1024 ** 3,
      by_memory: 28,
      by_cpu: 48,
      limited_by: "memory",
      recommended: 28,
    },
    running: 0,
    ...over,
  }) as unknown as ResourceSettings;

async function gotoSystemStep(settings = systemSettings()) {
  vi.mocked(api.resources).mockResolvedValue({ settings });
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await screen.findByTestId("setup-system-reason");
}

test("setup explains the limit it picked: the machine, the number and the reason", async () => {
  await gotoSystemStep();
  expect(screen.getByTestId("session-capacity-facts")).toHaveTextContent("8");
  expect(screen.getByTestId("session-capacity-facts")).toHaveTextContent("32 GiB");
  expect(screen.getByTestId("session-capacity-facts")).toHaveTextContent("28");
  expect(screen.getByTestId("setup-system-reason")).toHaveTextContent(
    "Memory sets this limit: 1 GiB per session, after keeping 3 GiB for the system. CPUs alone would allow 48",
  );
  expect(screen.getByText(/When 28 are running/)).toHaveTextContent("Subagents");
});

test("Next without an edit saves nothing: the limit keeps following the recommendation", async () => {
  await gotoSystemStep();
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await screen.findByText("Set up your AI");
  expect(api.setResources).not.toHaveBeenCalled();
});

test("an edited limit is saved, and an out-of-range one can't leave the step", async () => {
  vi.mocked(api.setResources).mockResolvedValue({ settings: systemSettings() });
  await gotoSystemStep();
  const input = screen.getByTestId("setup-system-limit");
  await userEvent.clear(input);
  await userEvent.type(input, "999");
  expect(screen.getByRole("button", { name: /^next$/i })).toBeDisabled();
  await userEvent.clear(input);
  await userEvent.type(input, "12");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await screen.findByText("Set up your AI");
  expect(api.setResources).toHaveBeenCalledWith({ max_sessions: 12 });
});

test("a re-run shows the saved limit next to the recommendation", async () => {
  await gotoSystemStep(
    systemSettings({
      values: { max_sessions: 60 } as ResourceSettings["values"],
      sources: { max_sessions: "settings" } as ResourceSettings["sources"],
    }),
  );
  expect(screen.getByTestId("setup-system-limit")).toHaveValue(60);
  expect(screen.getByText(/You set 60\. Recommended for this system: 28\./)).toBeInTheDocument();
});

test("Next waits for the explanation, and a read that never answers doesn't trap setup", async () => {
  vi.mocked(api.resources).mockReturnValue(new Promise(() => {}));
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  expect(screen.getByText("Reading this system…")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^next$/i })).toBeDisabled();
  await screen.findByRole("alert", undefined, { timeout: 12_000 });
  expect(screen.getByRole("button", { name: /^next$/i })).toBeEnabled();
}, 20_000);

test("an unreadable answer never traps setup", async () => {
  vi.mocked(api.resources).mockRejectedValue(new Error("no"));
  renderWizard();
  await userEvent.click(screen.getByRole("button", { name: /get started/i }));
  await userEvent.click(screen.getByRole("button", { name: /^continue$/i }));
  await screen.findByText("claude");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: /^next$/i }));
  await screen.findByText("Set up your AI");
});
