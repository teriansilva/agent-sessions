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
  EngineInfo,
} from "../types/api";
import {
  AgentDefaultsPage,
  AgentDetail,
  AgentsRoster,
} from "./AgentsSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      engines: vi.fn(),
      engineDetail: vi.fn(),
      agentUsage: vi.fn(),
      agentUsageRefresh: vi.fn(),
      setAgentBudgets: vi.fn(),
      setAgentDefaults: vi.fn(),
    },
  };
});

const FIXTURE = fixture.engines as EngineInfo[];
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

const card = (label: string) =>
  screen.getByRole("heading", { name: label }).closest("li")!;

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
  // Stale, not blank: every card of the last roster is still on screen.
  expect(
    within(screen.getByRole("list", { name: "Agents" })).getAllByRole(
      "heading",
      { level: 3 },
    ),
  ).toHaveLength(FIXTURE.length);
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

test("roster: an invalid manifest is a card with its source and exact error, and no actions", async () => {
  vi.mocked(api.engines).mockResolvedValue({
    engines: FIXTURE,
    problems: [PROBLEM],
  });
  renderPage(<AgentsRoster />);
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "acme-agent" })).toBeInTheDocument(),
  );
  const bad = card("acme-agent");
  expect(within(bad).getByText("Invalid manifest")).toBeInTheDocument();
  expect(within(bad).getByText(/must be the plugin id/)).toHaveTextContent(
    PROBLEM.source,
  );
  expect(within(bad).queryByRole("link")).toBeNull();
  expect(within(bad).queryByRole("button")).toBeNull();
  expect(screen.getByText(/loaded ·/)).toHaveTextContent(
    "Agents // 8 loaded · 0 retiring · 1 invalid",
  );
});

test("roster: a retiring engine shows why, and offers nothing to start", async () => {
  vi.mocked(api.engines).mockResolvedValue({
    engines: [...FIXTURE, RETIRING],
    problems: [],
  });
  renderPage(<AgentsRoster />);
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "legacy-cli" })).toBeInTheDocument(),
  );
  const c = card("legacy-cli");
  expect(within(c).getByText("Retiring")).toBeInTheDocument();
  expect(within(c).getByText(/agent removed/)).toBeInTheDocument();
  const caps = within(c).getByRole("list", { name: /capabilities/ });
  for (const chip of within(caps).getAllByRole("listitem")) {
    expect(chip).toHaveTextContent("(off)");
  }
  expect(screen.getByText(/loaded ·/)).toHaveTextContent(
    "Agents // 8 loaded · 1 retiring · 0 invalid",
  );
});

// ---- an agent's own page ------------------------------------------------------------------------

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
  expect(screen.getByText("No binary")).toBeInTheDocument();
  expect(screen.getByText(/runs no process: BattleLab talks to its endpoint/)).toHaveTextContent(
    "(openai-chat)",
  );
  expect(screen.getByText("none — runs no process")).toBeInTheDocument();
});

test("agent page: identity, provenance, store, capabilities, and an honest empty models list", async () => {
  renderPage(<AgentDetail id="claude" />);
  expect(
    await screen.findByRole("heading", { name: "Claude Code" }),
  ).toBeInTheDocument();
  expect(api.engineDetail).toHaveBeenCalledWith("claude");
  expect(screen.getByText("/home/op/.local/bin/claude")).toBeInTheDocument();
  expect(screen.getByText("AGENT_SESSIONS_CLAUDE_BIN")).toBeInTheDocument();
  expect(screen.getByText("claude-projects")).toBeInTheDocument();
  expect(screen.getByText("read-only")).toBeInTheDocument();
  expect(screen.getByText("None declared")).toBeInTheDocument();
  const caps = screen
    .getByRole("heading", { name: "Capabilities" })
    .closest("section")!;
  expect(within(caps).getAllByRole("listitem")).toHaveLength(8);
  // No per-agent bypass: the defaults are global and this page points at them.
  expect(screen.queryByRole("switch")).toBeNull();
  expect(
    screen.getByRole("link", { name: /Agents › Defaults/ }),
  ).toHaveAttribute("href", "/settings/agents/defaults");
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
