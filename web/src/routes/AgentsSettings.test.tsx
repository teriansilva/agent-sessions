/** Settings → AGENTS (#853 P4, #1128): the roster, an agent's own page, and the defaults — each
 *  page's states, read from a mocked `/api/engines`, `/api/engines/{id}` and `/api/config`. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { getRoster, resetRoster, setRoster } from "../app/engineRoster";
import { api, ApiError } from "../lib/api";
import fixture from "../test/roster.fixture.json";
import type {
  AgentDefaults,
  AppConfig,
  EngineDetail,
  EngineInfo, AgentEndpoint } from "../types/api";
import {
  AgentDefaultsPage,
  AgentDetail,
  AgentsRoster,
} from "./AgentsSettings";
import { buildRows } from "../components/agents/agentStatus";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      engines: vi.fn(),
      plugins: vi.fn().mockResolvedValue({ feed: { state: "missing", error: null }, catalog: [], plugins: [], operations: [], roster_generation: 1 }),
      engineDetail: vi.fn(),
      agentUsage: vi.fn(),
      agentUsageRefresh: vi.fn(),
      setAgentBudgets: vi.fn(),
      setAgentDefaults: vi.fn(),
      agentEndpoint: vi.fn(),
      setAgentEndpoint: vi.fn(),
      testAgentEndpoint: vi.fn(),
      pluginReload: vi.fn(),
    },
  };
});

const FIXTURE = fixture.engines as EngineInfo[];
const ENDPOINT: AgentEndpoint = {
  base_url: "https://llm.example.lan/v1",
  model: "qwen3-coder",
  api_key_set: true,
  context_window: 32768,
  max_output_tokens: 4096,
  request_timeout: null,
  configured: true,
};
const byId = (id: string) => FIXTURE.find((e) => e.id === id)!;

const RETIRING: EngineInfo = {
  ...byId("kimi"),
  id: "legacy-cli",
  label: "legacy-cli",
  display: { ...byId("kimi").display, name: "legacy-cli", badge: "lc" },
  status: "retiring",
  status_reason: "agent removed — its running sessions stay attachable until they exit",
};

const PROBLEM = {
  source: "plugins/first_party/acme-agent/plugin.toml",
  error: "binary.name: must be the plugin id or one of binary.aliases",
};

const DETAIL: EngineDetail = {
  id: "claude",
  label: "Claude Code",
  publisher: "battlelab",
  version: "1",
  contract: 1,
  source: "in-tree",
  kind: "agent",
  runtime: "pty",
  binary: {
    name: "claude",
    env_var: "AGENT_SESSIONS_CLAUDE_BIN",
    search_paths: ["~/.local/bin"],
  },
  provenance: {
    state: "adopted",
    via: "search_paths",
    path: "/home/op/.local/bin/claude",
    note: null,
  },
  store: {
    root: "~/.claude",
    resolved: "/home/op/.claude",
    layout: "claude-projects",
    read_only: true,
  },
  launch: { resume: "flag", new: "pin-flag", admission: null },
  transcript: { kind: "claude-jsonl", strict: false },
  usage: { source: "plan", kind: "claude-cli-probe" },
  capabilities: { ...byId("claude").capabilities },
  models: [],
  maintenance: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.engines).mockResolvedValue({
    engines: FIXTURE,
    problems: [],
  });
  vi.mocked(api.agentUsage).mockResolvedValue({
    budgets: { threshold_pct: 90, notify: true, engines: {} },
    agents: [],
  });
  vi.mocked(api.engineDetail).mockResolvedValue(DETAIL);
  vi.mocked(api.agentEndpoint).mockResolvedValue(ENDPOINT);
});

function renderPage(
  node: React.ReactNode,
  defaults?: AgentDefaults,
  refresh: () => void = () => {},
) {
  const config = {
    csrf: "x",
    new_session_engines: FIXTURE.map((e) => e.id),
    terminal_backend: "ws",
    ...(defaults ? { agent_defaults: defaults } : {}),
  } as AppConfig;
  return render(
    <ConfigCtx.Provider value={config}>
      <ConfigRefreshCtx.Provider value={refresh}>
        <MemoryRouter>{node}</MemoryRouter>
      </ConfigRefreshCtx.Provider>
    </ConfigCtx.Provider>,
  );
}

/** One roster row (#1408), found by its link — or its label, for a row without a page. */
const card = (label: string) =>
  (screen.queryByRole("link", { name: `Details for ${label}` }) ?? screen.getByText(label)).closest("li")!;
const rosterList = () => screen.getByRole("list", { name: "All agents" });

// ---- roster -------------------------------------------------------------------------------------

test("roster: says it is loading until a roster has ever arrived", async () => {
  resetRoster();
  vi.mocked(api.engines).mockReturnValue(new Promise(() => {}));
  renderPage(<AgentsRoster />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading agents…");
  expect(screen.queryByRole("list", { name: "Agents" })).toBeNull();
});

test("roster: a failed refresh KEEPS the last good list, says so, and retries", async () => {
  vi.mocked(api.engines).mockRejectedValueOnce(new Error("offline"));
  renderPage(<AgentsRoster />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t refresh the agent list — showing the last one loaded.",
  );
  // Stale, not blank: every row of the last roster is still on screen (one per CLI, #1408).
  expect(within(rosterList()).getAllByRole("listitem")).toHaveLength(
    buildRows(FIXTURE, [], null).length,
  );
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(api.engines).toHaveBeenCalledTimes(2);
});

test("roster: a failed FIRST load says nothing was loaded", async () => {
  resetRoster();
  vi.mocked(api.engines).mockRejectedValue(new Error("offline"));
  renderPage(<AgentsRoster />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t load the agent list.",
  );
});

test("roster: an EMPTY roster is a success, distinct from a failure", async () => {
  vi.mocked(api.engines).mockResolvedValue({ engines: [], problems: [] });
  renderPage(<AgentsRoster />);
  expect(await screen.findByText("No agents loaded")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(getRoster().status).toBe("ready");
});

test("roster: an invalid manifest is an Invalid row whose Details show its source and exact error", async () => {
  vi.mocked(api.engines).mockResolvedValue({
    engines: FIXTURE,
    problems: [PROBLEM],
  });
  renderPage(<AgentsRoster />);
  await waitFor(() => expect(screen.getByText("acme-agent")).toBeInTheDocument());
  const bad = card("acme-agent");
  expect(within(bad).getByText("Invalid")).toBeInTheDocument();
  // No page of its own: nothing to link to.
  expect(within(bad).queryByRole("link")).toBeNull();
  await userEvent.click(within(bad).getByRole("button", { name: "Details" }));
  const diag = within(bad).getByRole("region", { name: /manifest error/ });
  expect(diag).toHaveTextContent(PROBLEM.source);
  expect(diag).toHaveTextContent(/must be the plugin id/);
  // Invalid needs attention and is never counted ready.
  const rows = buildRows(FIXTURE, [PROBLEM], null);
  const ready = rows.filter((r) => r.status.rank === 8).length;
  expect(screen.getByTestId("roster-summary")).toHaveTextContent(
    `${ready} ready · ${rows.length - ready} need attention`,
  );
});

test("roster: a retiring engine reads Retiring, needs attention, and offers nothing to start", async () => {
  vi.mocked(api.engines).mockResolvedValue({
    engines: [...FIXTURE, RETIRING],
    problems: [],
  });
  renderPage(<AgentsRoster />);
  await waitFor(() => expect(screen.getByRole("link", { name: "Details for legacy-cli" })).toBeInTheDocument());
  const c = card("legacy-cli");
  expect(within(c).getByText("Retiring")).toBeInTheDocument();
  // The only links are its page and its mode chip — no setup or launch action.
  for (const link of within(c).getAllByRole("link")) {
    expect(link.getAttribute("href")).toMatch(/^\/settings\/agents\/legacy-cli$/);
  }
  await userEvent.click(screen.getByRole("button", { name: /Needs attention/ }));
  expect(within(rosterList()).getByRole("link", { name: "Details for legacy-cli" })).toBeInTheDocument();
});

test("agent page: a chat-runtime agent shows no binary and no launch, not a crash (#1209)", async () => {
  vi.mocked(api.engineDetail).mockResolvedValue({
    ...DETAIL,
    runtime: "chat",
    binary: null,
    launch: null,
    endpoint: { kind: "openai-chat" },
    provenance: { state: "absent", via: null, path: null, note: null },
  });
  renderPage(<AgentDetail id="claude" />);
  expect(
    await screen.findByRole("heading", { name: "Claude Code" }),
  ).toBeInTheDocument();
  expect(screen.getByText(/BattleLab talks to its endpoint and runs nothing on this host/)).toBeInTheDocument();
  expect(screen.getByText("No binary of its own")).toBeInTheDocument();
  expect(screen.getByText(/BattleLab talks to its endpoint \(openai-chat\)/)).toBeInTheDocument();
  expect(screen.getByText("none — runs no process of its own")).toBeInTheDocument();
});

test("agent page: status and actions first; identity, provenance, store and capabilities folded away (#1408)", async () => {
  renderPage(<AgentDetail id="claude" />);
  expect(
    await screen.findByRole("heading", { name: "Claude Code" }),
  ).toBeInTheDocument();
  expect(api.engineDetail).toHaveBeenCalledWith("claude");
  // The header carries the status and the ⋯ menu; the technical sections are in a CLOSED
  // disclosure (still in the document, so a reader can open them).
  expect(screen.getByRole("button", { name: /More actions for Claude Code/ })).toBeInTheDocument();
  const tech = screen.getByTestId("technical-details") as HTMLDetailsElement;
  expect(tech.open).toBe(false);
  expect(within(tech).getByText("AGENT_SESSIONS_CLAUDE_BIN")).toBeInTheDocument();
  expect(within(tech).getByText("claude-projects")).toBeInTheDocument();
  expect(within(tech).getByText("read-only")).toBeInTheDocument();
  expect(within(tech).getByText("None declared")).toBeInTheDocument();
  const caps = within(tech).getByText("Capabilities").closest("section")!;
  expect(within(caps).getAllByRole("listitem")).toHaveLength(8);
  // The resolved path is in the header too, where an operator looks first.
  expect(screen.getAllByText("/home/op/.local/bin/claude").length).toBeGreaterThan(0);
  // No per-agent bypass: the defaults are global.
  expect(screen.queryByRole("switch")).toBeNull();
});

test("agent page: a 404 is 'no such agent', not an error or a blank page", async () => {
  vi.mocked(api.engineDetail).mockRejectedValue(
    new ApiError(404, "GET /api/engines/nope → 404"),
  );
  renderPage(<AgentDetail id="nope" />);
  expect(
    await screen.findByRole("heading", { name: "No such agent" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /All agents/ })).toHaveAttribute(
    "href",
    "/settings/agents",
  );
});

test("agent page: any other failure offers a retry", async () => {
  vi.mocked(api.engineDetail).mockRejectedValueOnce(new Error("boom"));
  renderPage(<AgentDetail id="claude" />);
  await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
  expect(
    await screen.findByRole("heading", { name: "Claude Code" }),
  ).toBeInTheDocument();
});

// ---- defaults -----------------------------------------------------------------------------------

test("defaults: lists only engines eligible for a new session, the stored one checked", () => {
  setRoster(
    FIXTURE.map((e) => (e.id === "codex" ? { ...e, present: false } : e)),
  );
  renderPage(<AgentDefaultsPage />, { default_engine: "opencode", bypass: true });
  const group = screen.getByRole("group", {
    name: "Default agent for new sessions",
  });
  const radios = within(group).getAllByRole("radio");
  expect(radios.map((r) => (r as HTMLInputElement).value)).not.toContain(
    "codex",
  );
  expect(within(group).getByRole("radio", { name: /opencode/ })).toBeChecked();
  expect(screen.queryByText("Default unavailable")).toBeNull();
});

test("defaults: an uninstalled default is kept, shown greyed, and the fallback is named", () => {
  setRoster(
    FIXTURE.map((e) =>
      e.id === "gemini" ? { ...e, present: false, bin: null } : e,
    ),
  );
  renderPage(<AgentDefaultsPage />, { default_engine: "gemini", bypass: true });
  expect(screen.getByRole("status")).toHaveTextContent(
    "Your default, Gemini CLI, is not installed — new sessions use Claude Code until it is. Your choice is kept.",
  );
  const gone = screen.getByRole("radio", { name: /Gemini CLI/ });
  expect(gone).toBeDisabled();
  expect(gone).toBeChecked();
  expect(gone.closest("label")).toHaveTextContent("not installed");
  // Nothing changed, so there is nothing to save — the stored choice is never rewritten by a visit.
  expect(screen.getByRole("button", { name: "Save defaults" })).toBeDisabled();
});

test("defaults: saving bypass sends ONLY bypass, even with an uninstalled default stored", async () => {
  setRoster(
    FIXTURE.map((e) =>
      e.id === "gemini" ? { ...e, present: false, bin: null } : e,
    ),
  );
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: "gemini", bypass: false },
  });
  const refresh = vi.fn();
  renderPage(
    <AgentDefaultsPage />,
    { default_engine: "gemini", bypass: true },
    refresh,
  );
  await userEvent.click(
    screen.getByRole("switch", { name: /permission bypass/i }),
  );
  await userEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  expect(api.setAgentDefaults).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.setAgentDefaults).mock.calls[0][0]).toEqual({
    bypass: false,
  });
  // Every useConfig() consumer re-reads the saved values (stale-ConfigCtx rule).
  expect(refresh).toHaveBeenCalled();
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
});

test("defaults: picking an agent sends only default_engine", async () => {
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: "codex", bypass: true },
  });
  renderPage(<AgentDefaultsPage />, { default_engine: null, bypass: true });
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  await userEvent.click(screen.getByRole("button", { name: "Save defaults" }));
  expect(vi.mocked(api.setAgentDefaults).mock.calls[0][0]).toEqual({
    default_engine: "codex",
  });
});

// Draft ownership (Hermes on #1163): rerendered with a NEW config object each time, exactly as
// ConfigProvider publishes a refresh.
function defaultsTree(d: AgentDefaults, refresh: () => void = () => {}) {
  const config = {
    csrf: "x",
    new_session_engines: FIXTURE.map((e) => e.id),
    terminal_backend: "ws",
    agent_defaults: d,
  } as AppConfig;
  return (
    <ConfigCtx.Provider value={config}>
      <ConfigRefreshCtx.Provider value={refresh}>
        <MemoryRouter>
          <AgentDefaultsPage />
        </MemoryRouter>
      </ConfigRefreshCtx.Provider>
    </ConfigCtx.Provider>
  );
}
const saveBtn = () => screen.getByRole("button", { name: "Save defaults" });

test("defaults: another tab's change spends a draft — the newer value shows through", async () => {
  const { rerender } = render(defaultsTree({ default_engine: "claude", bypass: true }));
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  expect(saveBtn()).toBeEnabled();
  rerender(defaultsTree({ default_engine: "kimi", bypass: true }));
  expect(screen.getByRole("radio", { name: /Kimi/ })).toBeChecked();
  expect(saveBtn()).toBeDisabled();
});

test("defaults: A → B → A does NOT revive a spent draft into an unrelated save", async () => {
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: "claude", bypass: false },
  });
  const { rerender } = render(defaultsTree({ default_engine: "claude", bypass: true }));
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  rerender(defaultsTree({ default_engine: "kimi", bypass: true }));
  rerender(defaultsTree({ default_engine: "claude", bypass: true }));
  // Back at the draft's own baseline — and the draft stays spent.
  expect(screen.getByRole("radio", { name: /Claude/ })).toBeChecked();
  expect(saveBtn()).toBeDisabled();
  await userEvent.click(screen.getByRole("switch", { name: /permission bypass/i }));
  await userEvent.click(saveBtn());
  expect(vi.mocked(api.setAgentDefaults).mock.calls[0][0]).toEqual({ bypass: false });
});

test("defaults: nothing can be picked while a save is in flight, and a pick made after it survives its refresh", async () => {
  let resolve!: (v: { agent_defaults: AgentDefaults }) => void;
  vi.mocked(api.setAgentDefaults).mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  const refresh = vi.fn();
  const { rerender } = render(
    defaultsTree({ default_engine: "claude", bypass: true }, refresh),
  );
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  await userEvent.click(saveBtn());
  // In flight: the pickers are locked, so no newer edit can be dropped by the save's refresh.
  expect(screen.getByRole("radio", { name: /Kimi/ })).toBeDisabled();
  expect(screen.getByRole("switch", { name: /permission bypass/i })).toBeDisabled();
  await act(async () => resolve({ agent_defaults: { default_engine: "codex", bypass: true } }));
  // Resolved, refresh not yet landed: the server's answer is shown, and a new pick is allowed.
  expect(screen.getByRole("radio", { name: /Codex/ })).toBeChecked();
  expect(refresh).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("radio", { name: /Kimi/ }));
  // The refresh our save triggered lands: it is OURS, so the Kimi pick survives it.
  rerender(defaultsTree({ default_engine: "codex", bypass: true }, refresh));
  expect(screen.getByRole("radio", { name: /Kimi/ })).toBeChecked();
  expect(saveBtn()).toBeEnabled();
});

test("defaults: after a save whose refresh FAILED, the next foreign change still spends a draft", async () => {
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: "codex", bypass: true },
  });
  const { rerender } = render(defaultsTree({ default_engine: "claude", bypass: true }));
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  await userEvent.click(saveBtn());
  await screen.findByText("Saved.");
  // The refresh our save asked for never lands (a network blip). A pick is made meanwhile…
  await userEvent.click(screen.getByRole("radio", { name: /Kimi/ }));
  // …and then ANOTHER tab's change arrives. It does not carry our saved defaults, so it is not
  // ours: the Kimi draft is spent and Gemini — what is really stored — shows through.
  rerender(defaultsTree({ default_engine: "gemini", bypass: true }));
  expect(screen.getByRole("radio", { name: /Gemini/ })).toBeChecked();
  expect(saveBtn()).toBeDisabled();
});

test("defaults: an earlier save's refresh landing during the next save never hides that save's answer", async () => {
  let resolve2!: (v: { agent_defaults: AgentDefaults }) => void;
  vi.mocked(api.setAgentDefaults)
    .mockResolvedValueOnce({ agent_defaults: { default_engine: "codex", bypass: true } })
    .mockReturnValueOnce(
      new Promise((r) => {
        resolve2 = r;
      }),
    );
  const { rerender } = render(defaultsTree({ default_engine: "claude", bypass: true }));
  // Save 1 (Codex) answers; its refresh is still pending.
  await userEvent.click(screen.getByRole("radio", { name: /Codex/ }));
  await userEvent.click(saveBtn());
  await screen.findByText("Saved.");
  // Save 2 (Kimi) goes out…
  await userEvent.click(screen.getByRole("radio", { name: /Kimi/ }));
  await userEvent.click(saveBtn());
  // …save 1's refresh lands while it is in flight…
  rerender(defaultsTree({ default_engine: "codex", bypass: true }));
  // …and save 2 answers Kimi. Its own refresh then never lands (fails): Kimi is still what shows.
  await act(async () => resolve2({ agent_defaults: { default_engine: "kimi", bypass: true } }));
  expect(screen.getByRole("radio", { name: /Kimi/ })).toBeChecked();
  expect(saveBtn()).toBeDisabled();
  expect(vi.mocked(api.setAgentDefaults).mock.calls.map((c) => c[0])).toEqual([
    { default_engine: "codex" },
    { default_engine: "kimi" },
  ]);
});

test("defaults: a refused save shows the server's words", async () => {
  vi.mocked(api.setAgentDefaults).mockRejectedValue(
    new ApiError(422, "agent_defaults.bypass must be a boolean"),
  );
  renderPage(<AgentDefaultsPage />, { default_engine: null, bypass: true });
  await userEvent.click(
    screen.getByRole("switch", { name: /permission bypass/i }),
  );
  await act(async () => {
    await userEvent.click(
      screen.getByRole("button", { name: "Save defaults" }),
    );
  });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "agent_defaults.bypass must be a boolean",
  );
});

// ---- #1189: operator-added model ids ------------------------------------------------------------

const WITH_MODELS: EngineDetail = {
  ...DETAIL,
  models: byId("claude").models,
  model_select: {
    supported: true,
    on_resume: true,
    configured_elsewhere: false,
    offered: [
      ...byId("claude").model_select!.offered,
      { id: "claude-next", aliases: [], context_window: null, source: "operator" },
    ],
  },
};

test("agent page: added model ids are listed, and adding one saves the WHOLE list (#1189)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.engineDetail).mockResolvedValue(WITH_MODELS);
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: null, bypass: true },
  });
  renderPage(<AgentDetail id="claude" />);
  const box = await screen.findByTestId("added-models");
  expect(within(box).getByText("claude-next")).toBeInTheDocument();
  // The manifest's own models are not in the operator list.
  expect(within(box).queryByText("claude-opus-5")).not.toBeInTheDocument();
  await user.type(within(box).getByRole("textbox", { name: "Model id to add" }), "claude-later");
  await user.click(within(box).getByRole("button", { name: "Add model" }));
  expect(vi.mocked(api.setAgentDefaults).mock.calls[0][0]).toEqual({
    models: { claude: ["claude-next", "claude-later"] },
  });
  // The remove is built from what the add STORED, not the stale prop (the detail refetch the
  // save triggered still says ["claude-next"]): the id just added survives.
  await user.click(within(box).getByRole("button", { name: "Remove claude-next" }));
  expect(vi.mocked(api.setAgentDefaults).mock.calls[1][0]).toEqual({
    models: { claude: ["claude-later"] },
  });
  expect(within(box).getByText("claude-later")).toBeInTheDocument();
  expect(within(box).queryByText("claude-next")).not.toBeInTheDocument();
});

test("agent page: model-list edits are serialised, and the save's answer is the list (#1189)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.engineDetail).mockResolvedValue(WITH_MODELS);
  let resolve!: (v: { agent_defaults: AgentDefaults }) => void;
  vi.mocked(api.setAgentDefaults).mockImplementationOnce(
    () => new Promise((r) => (resolve = r)),
  );
  renderPage(<AgentDetail id="claude" />);
  const box = await screen.findByTestId("added-models");
  await user.type(within(box).getByRole("textbox", { name: "Model id to add" }), "claude-later");
  await user.click(within(box).getByRole("button", { name: "Add model" }));
  // In flight: nothing else can be edited, so no second request can race the first.
  expect(within(box).getByRole("button", { name: "Remove claude-next" })).toBeDisabled();
  expect(within(box).getByRole("textbox", { name: "Model id to add" })).toBeDisabled();
  await act(async () => {
    // The server's stored list wins over what was sent.
    resolve({
      agent_defaults: {
        default_engine: null,
        bypass: true,
        models: { claude: ["claude-next", "claude-later", "claude-server"] },
      },
    });
  });
  expect(within(box).getByText("claude-server")).toBeInTheDocument();
  vi.mocked(api.setAgentDefaults).mockResolvedValue({
    agent_defaults: { default_engine: null, bypass: true },
  });
  await user.click(within(box).getByRole("button", { name: "Remove claude-next" }));
  expect(vi.mocked(api.setAgentDefaults).mock.calls).toHaveLength(2);
  expect(vi.mocked(api.setAgentDefaults).mock.calls[1][0]).toEqual({
    models: { claude: ["claude-later", "claude-server"] },
  });
});

test("agent page: a malformed id is refused before saving; a server refusal is shown (#1189)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.engineDetail).mockResolvedValue(WITH_MODELS);
  vi.mocked(api.setAgentDefaults).mockRejectedValue(
    new ApiError(422, "agent_defaults.models.claude: 'opus' is already offered by the agent"),
  );
  renderPage(<AgentDetail id="claude" />);
  const box = await screen.findByTestId("added-models");
  const input = within(box).getByRole("textbox", { name: "Model id to add" });
  await user.type(input, "--model=x");
  await user.click(within(box).getByRole("button", { name: "Add model" }));
  expect(api.setAgentDefaults).not.toHaveBeenCalled();
  expect(within(box).getByTestId("added-models-error")).toHaveTextContent("letters, digits");
  await user.clear(input);
  await user.type(input, "opus");
  await user.click(within(box).getByRole("button", { name: "Add model" }));
  expect(await within(box).findByTestId("added-models-error")).toHaveTextContent(
    "already offered",
  );
});

test("agent page: a configured-elsewhere agent offers no add field and says why (#1189)", async () => {
  vi.mocked(api.engineDetail).mockResolvedValue({
    ...DETAIL,
    id: "opencode",
    label: "opencode",
    model_select: { supported: false, on_resume: false, configured_elsewhere: true, offered: [] },
  });
  renderPage(<AgentDetail id="opencode" />);
  expect(await screen.findByText(/own configuration/)).toBeInTheDocument();
  expect(screen.queryByTestId("added-models")).not.toBeInTheDocument();
});

test("roster: an API client is a mode of its source's row — Ready or Unavailable, never 'not installed' (#1311, #1408)", async () => {
  const ready = { ...FIXTURE.find((e) => e.id === "codex-api")!, present: true, supports_new: true, api: { kind: "codex-app-server", source: "codex", unavailable_reason: null } };
  const down = { ...FIXTURE.find((e) => e.id === "claude-api")!, present: false, supports_new: false, api: { kind: "claude-stream-json", source: "claude", unavailable_reason: "claude 2.1.287 or later is required for native mode" } };
  vi.mocked(api.engines).mockResolvedValue({
    engines: FIXTURE.map((e) => (e.id === ready.id ? ready : e.id === down.id ? down : e)),
    problems: [],
  });
  renderPage(<AgentsRoster />);
  await waitFor(() => expect(screen.getByRole("link", { name: "Details for Codex" })).toBeInTheDocument());
  const codexRow = card("Codex");
  expect(within(codexRow).getByRole("link", { name: "Codex — API: API, Ready" })).toHaveAttribute(
    "href",
    "/settings/agents/codex-api",
  );
  const claudeRow = card("Claude Code");
  expect(within(claudeRow).getByRole("link", { name: "Claude — API: API, Unavailable" })).toBeInTheDocument();
  // The row reads its WORST mode's word.
  expect(within(claudeRow).getByText("Unavailable")).toBeInTheDocument();
  expect(within(claudeRow).queryByText(/not installed/i)).toBeNull();
});

test("agent page: an unavailable API client says what it uses and why it cannot start (#1408)", async () => {
  const down = { ...FIXTURE.find((e) => e.id === "claude-api")!, present: false, api: { kind: "claude-stream-json", source: "claude", unavailable_reason: "claude 2.1.287 or later is required for native mode" } };
  vi.mocked(api.engines).mockResolvedValue({ engines: FIXTURE.map((e) => (e.id === down.id ? down : e)), problems: [] });
  vi.mocked(api.engineDetail).mockResolvedValue({
    ...DETAIL, id: "claude-api", label: "Claude — API", runtime: "api", binary: null, launch: null,
    provenance: { state: "absent", via: null, path: null, note: null },
  });
  setRoster(FIXTURE.map((e) => (e.id === down.id ? down : e)), []);
  renderPage(<AgentDetail id="claude-api" />);
  expect(await screen.findByTestId("api-client-status")).toHaveTextContent(
    "API client — uses your Claude Code installation, sign-in, providers and MCP servers",
  );
  expect(screen.getByText(/2\.1\.287 or later/)).toBeInTheDocument();
  // An API client has no binary of its own: no ABSENT chip, no "talks to its endpoint".
  expect(screen.queryByText(/^absent$/i)).toBeNull();
  expect(screen.queryByText(/talks to its endpoint/)).toBeNull();
});

test("agent page: a previous deactivation never follows the operator to another agent (#1416 review)", async () => {
  // React Router reuses AgentDetail across /settings/agents/<id> changes; the ⋯ menu must not.
  sessionStorage.setItem(
    "plugin-deactivate:claude",
    JSON.stringify({ request_id: "r-1", plugin_id: "claude", kind: "remove" }),
  );
  const { rerender } = renderPage(<AgentDetail id="claude" />);
  await userEvent.click(await screen.findByTestId("agent-menu"));
  expect(screen.getByRole("menuitem", { name: "Check previous remove" })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  rerender(
    <ConfigCtx.Provider value={{ csrf: "x", new_session_engines: [], terminal_backend: "ws" } as AppConfig}>
      <ConfigRefreshCtx.Provider value={() => {}}>
        <MemoryRouter>
          <AgentDetail id="codex" />
        </MemoryRouter>
      </ConfigRefreshCtx.Provider>
    </ConfigCtx.Provider>,
  );
  await waitFor(() => expect(api.engineDetail).toHaveBeenCalledWith("codex"));
  await userEvent.click(await screen.findByTestId("agent-menu"));
  expect(screen.queryByRole("menuitem", { name: "Check previous remove" })).toBeNull();
  sessionStorage.clear();
});


test("roster: with ONLY invalid definitions, the row itself reloads the roster once fixed (#1416 review)", async () => {
  vi.mocked(api.engines).mockResolvedValue({ engines: [], problems: [PROBLEM] });
  vi.mocked(api.pluginReload).mockResolvedValue({} as never);
  renderPage(<AgentsRoster />);
  await waitFor(() => expect(screen.getByText("acme-agent")).toBeInTheDocument());
  // No agent page exists to reload from: the invalid row carries the control.
  expect(screen.queryByRole("link", { name: /^Details for / })).toBeNull();
  await userEvent.click(within(card("acme-agent")).getByRole("button", { name: "Details" }));
  // The definition is fixed on disk; the reload brings the agent in and the problem goes.
  vi.mocked(api.engines).mockResolvedValue({ engines: FIXTURE.slice(0, 1), problems: [] });
  await userEvent.click(screen.getByRole("button", { name: "Reload agent roster" }));
  expect(api.pluginReload).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByText("acme-agent")).toBeNull());
  expect(screen.getByRole("link", { name: `Details for ${FIXTURE[0].label}` })).toBeInTheDocument();
});


test("roster: an invalid-only reload whose roster read FAILS keeps the rows and says so (#1416 review)", async () => {
  vi.mocked(api.engines).mockResolvedValue({ engines: [], problems: [PROBLEM] });
  vi.mocked(api.pluginReload).mockResolvedValue({} as never);
  renderPage(<AgentsRoster />);
  await waitFor(() => expect(screen.getByText("acme-agent")).toBeInTheDocument());
  await userEvent.click(within(card("acme-agent")).getByRole("button", { name: "Details" }));
  vi.mocked(api.engines).mockRejectedValueOnce(new Error("offline"));
  await userEvent.click(screen.getByRole("button", { name: "Reload agent roster" }));
  // The failure is reported on the row AND as the roster's own failure (with its Retry).
  expect(await within(card("acme-agent")).findByRole("alert")).toHaveTextContent("couldn’t read the agent list");
  expect(screen.getByText("Couldn’t refresh the agent list — showing the last one loaded.")).toBeInTheDocument();
  expect(screen.getByText("acme-agent")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reload agent roster" })).toBeEnabled();
});
