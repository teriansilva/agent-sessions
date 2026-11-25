/** HQ → Projects (#2143): the index, one project's General / Session defaults / Memory & equipment
 *  tabs. Ported from Settings → Projects' manager tests (the same calls, a new home), plus the
 *  states #2143 adds: a failed read is never an empty list, a partial defaults save says which half
 *  landed, and the memory switch reads one shared store. */
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { api, ApiError } from "../../lib/api";
import { resetProjectMemoryStore } from "../../lib/projectMemoryStore";
import { NEW_PROJECT_PATH, PROJECTS_PATH } from "../../lib/routes";
import type { AppConfig, ProjectArchiveReport, ProjectEntity } from "../../types/api";
import type { McpProjectStatus, McpServerSummary } from "../../types/mcp";
import ProjectPage from "./ProjectPage";
import ProjectsIndex from "./ProjectsIndex";

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      projectEntities: vi.fn(),
      patchProject: vi.fn(),
      deleteProject: vi.fn(),
      archiveProject: vi.fn(),
      unarchiveProject: vi.fn(),
      folders: vi.fn(),
      setPrefs: vi.fn(),
      memoryProjects: vi.fn(),
      setMemoryEnabled: vi.fn(),
      playbooks: vi.fn(),
      playbookProjects: vi.fn(),
      artifactActivation: vi.fn(),
      setArtifactActivation: vi.fn(),
      variables: vi.fn(),
      automations: vi.fn(),
      mcpServers: vi.fn(),
    },
  };
});
vi.mock("../../components/FolderPickerModal", () => ({
  FolderPickerModal: ({ onPick }: { onPick: (p: string) => void }) => (
    <div role="dialog" aria-label="folder picker">
      <button type="button" onClick={() => onPick("/picked")}>
        stub-pick
      </button>
    </div>
  ),
}));
// The defaults editor reads the engine roster and model lists; its own tests cover it.
vi.mock("../../components/projects/ProjectSessionDefaults", () => ({
  ProjectSessionDefaults: ({ onChange }: { onChange: (v: object) => void }) => (
    <button type="button" onClick={() => onChange({ bypass: false })}>
      stub-ask-every-time
    </button>
  ),
}));

function ent(over: Partial<ProjectEntity> = {}): ProjectEntity {
  return {
    id: "p-1",
    name: "SampleProject",
    color: "#5fd7ff",
    folders: ["/home/u/sampleproject"],
    default_folder: "/home/u/sampleproject",
    archived: false,
    created_at: 0,
    session_count: 3,
    ...over,
  };
}

function Probe() {
  const location = useLocation();
  return (
    <pre data-testid="probe">
      {location.pathname}
      {JSON.stringify(location.state)}
    </pre>
  );
}

function renderAt(path: string, config: Partial<AppConfig> = {}) {
  return render(
    <ConfigCtx.Provider value={{ csrf: "x", ...config } as AppConfig}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={PROJECTS_PATH} element={<ProjectsIndex />} />
          <Route path={`${PROJECTS_PATH}/:pid`} element={<ProjectPage />} />
          <Route path={NEW_PROJECT_PATH} element={<Probe />} />
          <Route path="*" element={<Probe />} />
        </Routes>
      </MemoryRouter>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetProjectMemoryStore();
  vi.mocked(api.folders).mockResolvedValue({ folders: [] });
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [ent()] });
  vi.mocked(api.setPrefs).mockResolvedValue({});
  vi.mocked(api.memoryProjects).mockResolvedValue({
    projects: [{ id: "p-1", name: "SampleProject", color: "", enabled: true, notes: 42, pending: 3, quarantined: 0, last_write: null }],
  });
  vi.mocked(api.playbooks).mockResolvedValue({ playbooks: [], default: null, recovery_total: 0 });
  vi.mocked(api.artifactActivation).mockResolvedValue({ mode: null } as never);
  vi.mocked(api.variables).mockResolvedValue({ variables: [], collections: [] } as never);
  vi.mocked(api.automations).mockResolvedValue({ automations: [] } as never);
  vi.mocked(api.mcpServers).mockResolvedValue({ servers: [] });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------------------------------- index

test("the index lists every project as ONE link to its page, archived ones requested too", async () => {
  renderAt(PROJECTS_PATH, { default_project_id: "p-1" });
  const row = await screen.findByRole("link", { name: /SampleProject/ });
  expect(row).toHaveAttribute("href", "/projects/p-1");
  // Nothing inside a row is a second control.
  expect(within(row).queryByRole("button")).toBeNull();
  expect(within(row).getByText("(default project)")).toBeInTheDocument();
  expect(within(row).getByText("~/sampleproject")).toBeInTheDocument();
  expect(api.projectEntities).toHaveBeenCalledWith({ includeArchived: true });
  expect(await within(row).findByText("42 notes")).toBeInTheDocument();
  expect(within(row).getByText("No playbook")).toBeInTheDocument();
  expect(screen.getByText(/never moves session files/i)).toBeInTheDocument();
});

test("the playbook column says when a newer revision waits, from the fleet read (never verify)", async () => {
  vi.mocked(api.playbooks).mockResolvedValue({
    playbooks: [{ id: "web-ops", ok: true, error: null, source: "local", editable: true, revision: "r4", default: false, name: "Web app ops", version: "4" }],
    default: null,
    recovery_total: 0,
  });
  vi.mocked(api.playbookProjects).mockResolvedValue({
    playbook_id: "web-ops",
    revision: "r4",
    projects: [{ project_id: "p-1", deployment_id: "d1", state: "applied", revision: "r3", update_available: true }],
  });
  renderAt(PROJECTS_PATH);
  const row = await screen.findByRole("link", { name: /SampleProject/ });
  expect(await within(row).findByText("Web app ops")).toBeInTheDocument();
  expect(within(row).getByText("Update → v4")).toBeInTheDocument();
});

test.each([
  ["bound", "Bound — not applied yet"],
  ["interrupted", "Interrupted — needs recovery"],
])("a %s deployment at the current revision is never drawn as current", async (state, words) => {
  vi.mocked(api.playbooks).mockResolvedValue({
    playbooks: [{ id: "web-ops", ok: true, error: null, source: "local", editable: true, revision: "r4", default: false, name: "Web app ops", version: "4" }],
    default: null,
    recovery_total: 0,
  });
  vi.mocked(api.playbookProjects).mockResolvedValue({
    playbook_id: "web-ops",
    revision: "r4",
    projects: [{ project_id: "p-1", deployment_id: "d1", state, revision: "r4", update_available: false }],
  });
  renderAt(PROJECTS_PATH);
  const row = await screen.findByRole("link", { name: /SampleProject/ });
  expect(await within(row).findByTestId("project-playbook-state")).toHaveTextContent(words);
  expect(within(row).queryByText("Current revision")).toBeNull();
  expect(within(row).queryByText("v4")).toBeNull();
});

test("New project opens the wizard with the `projects` return key (#1187)", async () => {
  const user = userEvent.setup();
  renderAt(PROJECTS_PATH);
  await screen.findByRole("link", { name: /SampleProject/ });
  await user.click(screen.getAllByRole("link", { name: /new project/i })[0]);
  expect(screen.getByTestId("probe")).toHaveTextContent(`${NEW_PROJECT_PATH}{"from":"projects"}`);
});

test("a failed list read says so with Retry, never the empty line; Retry reads again", async () => {
  const user = userEvent.setup();
  vi.mocked(api.projectEntities).mockRejectedValueOnce(new ApiError(503, "Service unavailable"));
  renderAt(PROJECTS_PATH);
  const failed = await screen.findByTestId("projects-read-failed");
  expect(failed).toHaveTextContent(/Couldn.t load projects/);
  expect(failed).toHaveTextContent(/HTTP 503/);
  expect(screen.queryByTestId("projects-empty")).toBeNull();
  await user.click(within(failed).getByRole("button", { name: /retry/i }));
  expect(await screen.findByRole("link", { name: /SampleProject/ })).toBeInTheDocument();
});

test("an empty list is the empty state, with New project", async () => {
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [] });
  renderAt(PROJECTS_PATH);
  const empty = await screen.findByTestId("projects-empty");
  expect(empty).toHaveTextContent(/No projects yet/);
  expect(screen.queryByTestId("projects-read-failed")).toBeNull();
});

test("archived projects sit in a collapsed Archived disclosure with Unarchive", async () => {
  const user = userEvent.setup();
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [ent(), ent({ id: "p-2", name: "OldThing", archived: true, folders: [] })],
  });
  const report: ProjectArchiveReport = { id: "p-2", archived: false, sessions: [], counts: { unarchived: 2 } };
  vi.mocked(api.unarchiveProject).mockResolvedValue(report);
  renderAt(PROJECTS_PATH);
  const toggle = await screen.findByRole("button", { name: /Archived \(1\)/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  await user.click(screen.getByRole("button", { name: "Unarchive project OldThing" }));
  expect(api.unarchiveProject).toHaveBeenCalledWith("p-2");
  expect(await screen.findByText(/Unarchive: 2 unarchived/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------------- the page: general

test("an unknown project says so (not a crash, not an error)", async () => {
  renderAt("/projects/p-nope");
  expect(await screen.findByTestId("project-missing")).toHaveTextContent(/No such project/);
});

test("changing the default folder patches default_folder (#448)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue(ent({ default_folder: "/picked" }));
  renderAt("/projects/p-1");
  await user.click(await screen.findByRole("button", { name: "Change default folder for SampleProject" }));
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { default_folder: "/picked" });
});

test("archive with a failed member shows the failed list and Retry re-calls the endpoint", async () => {
  const user = userEvent.setup();
  vi.mocked(api.archiveProject).mockResolvedValue({
    id: "p-1",
    archived: true,
    sessions: [
      { id: "claude:aaaa", result: "archived" },
      { id: "opencode:bbbb", result: "failed", reason: "db locked" },
    ],
    counts: { archived: 1, already_archived: 0, failed: 1 },
  });
  renderAt("/projects/p-1");
  await user.click(await screen.findByRole("button", { name: "Archive project SampleProject" }));
  expect(await screen.findByText(/1 archived · 0 already archived · 1 failed/)).toBeInTheDocument();
  expect(screen.getByText("opencode:bbbb")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(api.archiveProject).toHaveBeenCalledTimes(2);
});

test("delete asks in the shared confirm dialog, focus on the safe action; confirming deletes and returns to the index", async () => {
  const user = userEvent.setup();
  const native = vi.spyOn(window, "confirm");
  vi.mocked(api.deleteProject).mockResolvedValue({ deleted: true, id: "p-1" });
  renderAt("/projects/p-1");
  await user.click(await screen.findByRole("button", { name: "Delete project SampleProject" }));
  const dialog = await screen.findByRole("dialog", { name: /Delete project “SampleProject”\?/ });
  expect(dialog).toHaveTextContent(/never touched/i);
  expect(native).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /^cancel$/i })).toHaveFocus();
  await user.click(within(dialog).getByRole("button", { name: /^delete project$/i }));
  expect(api.deleteProject).toHaveBeenCalledWith("p-1");
  expect(await screen.findByTestId("projects-index")).toBeInTheDocument();
});

test("Cancel and Escape abort the delete and return focus to the trigger", async () => {
  const user = userEvent.setup();
  renderAt("/projects/p-1");
  const trigger = await screen.findByRole("button", { name: "Delete project SampleProject" });
  await user.click(trigger);
  await screen.findByRole("dialog");
  await user.click(screen.getByRole("button", { name: /^cancel$/i }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
  await user.click(trigger);
  await screen.findByRole("dialog");
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(api.deleteProject).not.toHaveBeenCalled();
});

test("a 409 folder conflict surfaces the server's detail inline", async () => {
  const user = userEvent.setup();
  vi.mocked(api.folders).mockResolvedValue({ folders: [{ cwd: "/home/u/free", label: "/home/u/free" }] } as never);
  vi.mocked(api.patchProject).mockRejectedValue(new ApiError(409, "folder /home/u/free is already adopted by BattleLab"));
  renderAt("/projects/p-1");
  const select = await screen.findByRole("combobox", { name: "Adopt a folder into SampleProject" });
  await user.selectOptions(select, "/home/u/free");
  await user.click(screen.getByRole("button", { name: /^add$/i }));
  expect(await screen.findByTestId("project-error")).toHaveTextContent("already adopted by BattleLab");
});

test("releasing a folder chip patches the remaining folder set", async () => {
  const user = userEvent.setup();
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [ent({ folders: ["/home/u/sampleproject", "/home/u/two"] })] });
  vi.mocked(api.patchProject).mockResolvedValue(ent());
  renderAt("/projects/p-1");
  await user.click(await screen.findByRole("button", { name: "Release folder /home/u/two from SampleProject" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { folders: ["/home/u/sampleproject"] });
});

test("rename commits via patchProject; a colour swatch sets and None clears the colour", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue(ent());
  renderAt("/projects/p-1");
  const name = await screen.findByRole("textbox", { name: "Project name for SampleProject" });
  await user.clear(name);
  await user.type(name, "Renamed{Enter}");
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { name: "Renamed" });
  const group = screen.getByRole("radiogroup", { name: "Color for SampleProject" });
  await user.click(within(group).getAllByRole("radio")[1]);
  expect(vi.mocked(api.patchProject).mock.calls.at(-1)?.[1]).toHaveProperty("color");
  await user.click(within(group).getByRole("radio", { name: "None" }));
  expect(api.patchProject).toHaveBeenLastCalledWith("p-1", { color: "" });
});

test("the default-project switch persists default_project_id, and clears it when switched off", async () => {
  const user = userEvent.setup();
  renderAt("/projects/p-1");
  const sw = await screen.findByRole("switch", { name: "Default project" });
  expect(sw).toHaveAttribute("aria-checked", "false");
  await user.click(sw);
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project_id: "p-1" });
  expect(sw).toHaveAttribute("aria-checked", "true");
  await user.click(sw);
  expect(api.setPrefs).toHaveBeenLastCalledWith({ default_project_id: "" });
});

test("a failed default-project save rolls back and says so", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("nope"));
  renderAt("/projects/p-1");
  const sw = await screen.findByRole("switch", { name: "Default project" });
  await user.click(sw);
  expect(await screen.findByText(/couldn.t save the default project/i)).toBeInTheDocument();
  expect(sw).toHaveAttribute("aria-checked", "false");
});

// -------------------------------------------------------------------------- the page: session defaults

test("Save writes the launch block and the artifacts default; both landing says Saved", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue(ent());
  vi.mocked(api.setArtifactActivation).mockResolvedValue({ mode: "private" } as never);
  renderAt("/projects/p-1?tab=defaults");
  await user.click(await screen.findByRole("button", { name: "stub-ask-every-time" }));
  await user.click(await screen.findByRole("radio", { name: /private/i }));
  await user.click(screen.getByRole("button", { name: "Save session defaults" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { launch_defaults: { bypass: false } });
  expect(api.setArtifactActivation).toHaveBeenCalledWith("project", "p-1", "private");
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
  expect(screen.queryByTestId("defaults-savebar")).toBeNull();
});

test("a partial save keeps the failed half unsaved with its error and never claims both saved", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue(ent());
  vi.mocked(api.setArtifactActivation).mockRejectedValue(new ApiError(503, "artifacts store unavailable"));
  renderAt("/projects/p-1?tab=defaults");
  await user.click(await screen.findByRole("button", { name: "stub-ask-every-time" }));
  await user.click(await screen.findByRole("radio", { name: /private/i }));
  await user.click(screen.getByRole("button", { name: "Save session defaults" }));
  const err = await screen.findByTestId("artifacts-save-error");
  expect(err).toHaveTextContent("Session defaults saved; the artifacts default didn’t: artifacts store unavailable");
  expect(screen.queryByText("Saved.")).toBeNull();
  // Only the artifacts half is still unsaved.
  expect(screen.getByTestId("defaults-savebar")).toHaveTextContent("artifacts default");
  expect(screen.getByTestId("defaults-savebar")).not.toHaveTextContent("session defaults ·");
});

test("the other direction: the launch block fails, the artifacts default lands — only the launch half stays unsaved", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockRejectedValue(new ApiError(500, "project store is read-only"));
  vi.mocked(api.setArtifactActivation).mockResolvedValue({ mode: "private" } as never);
  renderAt("/projects/p-1?tab=defaults");
  await user.click(await screen.findByRole("button", { name: "stub-ask-every-time" }));
  await user.click(await screen.findByRole("radio", { name: /private/i }));
  await user.click(screen.getByRole("button", { name: "Save session defaults" }));
  expect(await screen.findByTestId("defaults-save-error")).toHaveTextContent("project store is read-only");
  expect(api.setArtifactActivation).toHaveBeenCalledWith("project", "p-1", "private");
  expect(screen.queryByText("Saved.")).toBeNull();
  expect(screen.queryByTestId("artifacts-save-error")).toBeNull();
  const bar = screen.getByTestId("defaults-savebar");
  expect(bar).toHaveTextContent("session defaults");
  expect(bar).not.toHaveTextContent("artifacts default");
});

test("an unreadable artifacts default says so in place; the launch block still saves", async () => {
  vi.mocked(api.artifactActivation).mockRejectedValue(new ApiError(503, "down"));
  renderAt("/projects/p-1?tab=defaults");
  expect(await screen.findByTestId("artifacts-read-failed")).toHaveTextContent(/still save/);
});

// ------------------------------------------------------------------------ the page: memory & equipment

test("the memory switch writes the shared flag; a failed save goes back to the stored value", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setMemoryEnabled).mockRejectedValueOnce(new ApiError(500, "disk full"));
  renderAt("/projects/p-1?tab=memory");
  const sw = await screen.findByRole("switch", { name: "Shared memory for SampleProject" });
  await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "true"));
  await user.click(sw);
  expect(api.setMemoryEnabled).toHaveBeenCalledWith("p-1", false);
  expect(await screen.findByTestId("project-memory-note")).toHaveTextContent(/disk full/);
  // The server still says on: the switch shows what is stored.
  expect(sw).toHaveAttribute("aria-checked", "true");
  vi.mocked(api.setMemoryEnabled).mockResolvedValueOnce({ enabled: false });
  // The server now stores off: the re-read after the save answers that.
  vi.mocked(api.memoryProjects).mockResolvedValue({
    projects: [{ id: "p-1", name: "SampleProject", color: "", enabled: false, notes: 42, pending: 3, quarantined: 0, last_write: null }],
  });
  await user.click(sw);
  await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
});

test("bound secrets and targeting automations are listed read-only, each list failing on its own", async () => {
  vi.mocked(api.variables).mockResolvedValue({
    variables: [
      { id: "v1", name: "FORGEJO_TOKEN", kind: "secret", definition: { value_type: "text" }, set: true, projects: ["p-1"], created_at: 0, updated_at: 0 },
      { id: "v2", name: "OTHER", kind: "secret", definition: { value_type: "text" }, set: true, projects: ["p-9"], created_at: 0, updated_at: 0 },
    ],
    collections: [],
  } as never);
  vi.mocked(api.automations).mockRejectedValue(new ApiError(503, "down"));
  renderAt("/projects/p-1?tab=memory");
  const vars = await screen.findByRole("list", { name: "Variables bound to SampleProject" });
  expect(within(vars).getByText("FORGEJO_TOKEN")).toBeInTheDocument();
  expect(within(vars).queryByText("OTHER")).toBeNull();
  expect(await screen.findByTestId("automations-read-failed")).toBeInTheDocument();
  // The rest still works.
  expect(screen.getByRole("switch", { name: "Shared memory for SampleProject" })).toBeInTheDocument();
  await act(async () => {});
});

/** A server as `GET /api/mcp/servers?project=p-1` answers it: the summary plus p-1's own row. */
function mcp(name: string, over: Partial<McpServerSummary> = {}, row: Partial<McpProjectStatus> = {}): McpServerSummary {
  return {
    id: `id-${name}`,
    name,
    label: "",
    source: { kind: "npm", package: `${name}-mcp`, version: "1.2.3" },
    transport: "stdio",
    args: [],
    settings: [],
    project_settings: {},
    projects: "all",
    probe: null,
    enabled: true,
    rev: 1,
    installed: null,
    created_at: 0,
    updated_at: 0,
    tool_count: 0,
    enabled_projects: [],
    project_count: 1,
    state: { kind: "ready" },
    project: { project_id: "p-1", enabled: true, credentials: "shared", missing: [], check: "passed", checked_at: null, delivered: true, ...row },
    ...over,
  };
}

test("the project's MCP servers are listed read-only, each linking to its Library page (#2163 P2)", async () => {
  vi.mocked(api.mcpServers).mockResolvedValue({
    servers: [
      mcp("nextcloud", { tool_count: 38 }),
      mcp("sentry", { projects: ["p-1"], source: { kind: "remote", url: "https://mcp.sentry.dev/mcp" } }, { credentials: "own", check: "stale", delivered: false }),
      mcp("linear", {}, { credentials: "missing", missing: ["linear_key"], check: "needs_credentials", delivered: false }),
      mcp("paused", { enabled: false, projects: ["p-1"] }, { enabled: false, delivered: false }),
      mcp("elsewhere", { projects: ["p-9"] }, { enabled: false, credentials: "none_needed", check: "not_run", delivered: false }),
    ],
  });
  renderAt("/projects/p-1?tab=memory");
  const list = await screen.findByRole("list", { name: "MCP servers for SampleProject" });
  expect(api.mcpServers).toHaveBeenCalledWith("p-1");
  expect(within(list).getAllByRole("listitem")).toHaveLength(4);
  expect(within(list).queryByText("elsewhere")).toBeNull();
  const nc = within(list).getByTestId("project-mcp-nextcloud");
  expect(within(nc).getByRole("link", { name: "Open MCP server nextcloud" })).toHaveAttribute("href", "/library/mcp/id-nextcloud");
  expect(nc).toHaveTextContent("Shared");
  expect(nc).toHaveTextContent("npm · nextcloud-mcp 1.2.3 · 38 tools");
  expect(nc).toHaveTextContent("Passed");
  expect(within(list).getByTestId("project-mcp-sentry")).toHaveTextContent(/remote · mcp\.sentry\.dev.*Credentials changed — check again/);
  expect(within(list).getByTestId("project-mcp-linear")).toHaveTextContent("Needs linear_key");
  expect(within(list).getByTestId("project-mcp-paused")).toHaveTextContent("Disabled");
  // Three are on for the project; the disabled one is listed but not counted.
  expect(screen.getByText("3 enabled")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Manage in Library → MCP servers/ })).toHaveAttribute("href", "/library/mcp");
  // Read-only: no switch or button inside the section.
  expect(within(list).queryByRole("switch")).toBeNull();
  expect(within(list).queryByRole("button")).toBeNull();
});

test("no MCP server reads as empty; an unreadable registry fails in place, never as the empty line", async () => {
  renderAt("/projects/p-1?tab=memory");
  expect(await screen.findByText("No MCP server is enabled for this project.")).toBeInTheDocument();
  cleanup();
  vi.mocked(api.mcpServers).mockRejectedValue(new ApiError(503, "Service unavailable"));
  renderAt("/projects/p-1?tab=memory");
  expect(await screen.findByTestId("mcp-read-failed")).toHaveTextContent(/sessions keep the servers they started with/);
  expect(screen.queryByText("No MCP server is enabled for this project.")).toBeNull();
  // The rest of the tab still works.
  expect(screen.getByRole("switch", { name: "Shared memory for SampleProject" })).toBeInTheDocument();
  await act(async () => {});
});
