import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import { ApiError, api } from "../lib/api";
import { listenSessionLimit } from "../lib/sessionLimit";
import { mintNewSessionId } from "../lib/newSession";
import { projectPickerStore } from "../lib/projectPickerStore";
import { structuredModelsStore } from "../lib/structuredModelsStore";
import type { NewSessionDraft } from "../lib/newProject";
import { MAP_PATH, NEW_PROJECT_PATH } from "../lib/routes";
import { settingsPath } from "./settingsTabs";
import type { AppConfig, EngineInfo } from "../types/api";
import { NewSessionLanding } from "./NewSessionLanding";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      projectPickerEntities: vi.fn(),
      createProject: vi.fn(),
      setSessionProject: vi.fn(),
      chatNew: vi.fn(),
      structuredCreate: vi.fn(),
      structuredModels: vi.fn(),
      structuredSnapshot: vi.fn(),
      structuredStart: vi.fn(),
      // No pool (#1693): the form is a plain New session. The picker is pinned by
      // `e2e/new-session-specialist.spec.ts` and `lib/specialistLaunch.test.ts`.
      specialists: vi.fn(() => Promise.reject(new Error("no pool"))),
    },
  };
});
const mockEntities = vi.mocked(api.projectPickerEntities);

/** A stored "skip permission prompts" default of OFF. The API skip choice follows the stored default
 *  (#1473), so the tests that pin the GUARDED create path say so explicitly. */
const GUARDED: Partial<AppConfig> = { agent_defaults: { bypass: false } };

// The pickers are native radio groups (#1436). These read and drive them by VALUE, the way the
// old selects were driven, so a test names an engine or project id — never a display label.
const radioIn = (group: HTMLElement, value: string) =>
  (within(group).getAllByRole("radio") as HTMLInputElement[]).find((r) => r.value === value);
async function findProjects(): Promise<HTMLElement> {
  const group = await screen.findByRole("radiogroup", { name: "Project" });
  await within(group).findAllByRole("radio");
  return group;
}
const checkedIn = (name: string) =>
  (within(screen.getByRole("radiogroup", { name })).getAllByRole("radio") as HTMLInputElement[]).find(
    (r) => r.checked,
  )?.value;
const projectValue = () => checkedIn("Project");
const agentValue = () => checkedIn("Agent");
async function chooseProject(user: ReturnType<typeof userEvent.setup>, id: string) {
  const radio = radioIn(await findProjects(), id);
  if (!radio) throw new Error(`no project radio ${id}`);
  await user.click(radio);
}
async function chooseAgent(user: ReturnType<typeof userEvent.setup>, id: string) {
  const radio = radioIn(await screen.findByRole("radiogroup", { name: "Agent" }), id);
  if (!radio) throw new Error(`no agent radio ${id}`);
  await user.click(radio);
}
/** The resolved launch folder — the start summary carries it as `data-cwd` (#1458: the folder
 *  card is gone, the summary is where the launch folder is read). */
const launchFolder = () =>
  screen.getByTestId("new-session-summary").getAttribute("data-cwd");
/** Put keyboard focus inside the form without changing any choice — the shortcuts are scoped to
 *  it. The "No project" radio is always there, even while the list is still loading. */
const focusForm = () =>
  act(() =>
    (screen.getByRole("radio", { name: /no project/i }) as HTMLInputElement).focus(),
  );

const ENTITIES = [
  {
    id: "p-a",
    name: "Alpha",
    color: "",
    folders: ["/a"],
    default_folder: "/a",
    archived: false,
    created_at: 0,
    session_count: 1,
  },
  {
    id: "p-b",
    name: "Beta",
    color: "",
    folders: ["/b"],
    default_folder: "/b",
    archived: false,
    created_at: 0,
    session_count: 2,
  },
];

function renderLanding(
  engines = ["claude"],
  extra: Partial<AppConfig> = {},
  state: unknown = null,
) {
  const config: AppConfig = {
    csrf: "x",
    new_session_engines: engines,
    terminal_backend: "ws",
    ...extra,
  };
  return render(
    <ConfigCtx.Provider value={config}>
      <MemoryRouter initialEntries={[{ pathname: "/", state }]}>
        <Routes>
          <Route path="/" element={<NewSessionLanding />} />
        </Routes>
      </MemoryRouter>
    </ConfigCtx.Provider>,
  );
}

// What a native API client's CLI reports (#1313); the manifest-free roster has no list for it.
const CLI_MODELS = {
  status: "ok" as const,
  reason: null,
  models: [
    { id: "gpt-6.1-sol", label: "GPT-6.1 Sol", description: null, efforts: ["low"], is_default: true },
    { id: "gpt-6-luna", label: "gpt-6-luna", description: null, efforts: [], is_default: false },
  ],
};

beforeEach(() => {
  projectPickerStore.reset(); // the picker's cache outlives a render, as it outlives a route
  structuredModelsStore.reset(); // the model lists outlive a render too (#1503)
  navigateMock.mockReset();
  mockEntities.mockReset();
  mockEntities.mockResolvedValue({ projects: ENTITIES });
  vi.mocked(api.setSessionProject)
    .mockReset()
    .mockResolvedValue({ id: "x", project_id: "" });
  vi.mocked(api.createProject).mockReset();
  vi.mocked(api.structuredModels).mockReset().mockResolvedValue(CLI_MODELS);
  // An unresolved create is looked up on load (#1339): by default the server never made it.
  vi.mocked(api.structuredSnapshot)
    .mockReset()
    .mockRejectedValue(new ApiError(404, "no such native session"));
  vi.mocked(api.structuredStart).mockReset().mockResolvedValue({} as never);
});

test.each([
  ["claude", /^[0-9a-f-]{36}$/],
  ["codex", /^new-[0-9a-f-]{36}$/],
  ["gemini", /^[0-9a-f-]{36}$/],
  ["opencode", /^new-[0-9a-f-]{36}$/],
  // #454: antigravity reconciles (mints its own id) → MUST get the new-<uuid> placeholder, or
  // the ws new=1 launch rejects 4404 "session not found".
  ["antigravity", /^new-[0-9a-f-]{36}$/],
  // #636: shell is a PINNED-id engine (we mint the UUID and launch under it) → bare UUID, never
  // a placeholder, so the ws new=1 path treats it like claude.
  ["shell", /^[0-9a-f-]{36}$/],
])("mintNewSessionId(%s) → %s", (engine, shape) => {
  expect(mintNewSessionId(engine)).toMatch(shape);
});

test("the agent picker is hidden with one engine, shown with more (#448 reorder)", async () => {
  const { unmount } = renderLanding(["claude"]);
  expect(
    await findProjects(),
  ).toBeInTheDocument();
  expect(screen.queryByText("Agent")).not.toBeInTheDocument();
  unmount();
  renderLanding(["claude", "opencode"]);
  expect(await screen.findByText("Agent")).toBeInTheDocument();
});

test("selecting a project resolves its default folder and launches there (#448, #1458)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  expect(projectValue()).toBe("p-a"); // first entity is the default selection
  expect(
    launchFolder(),
  ).toBe("/a");

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path, opts] = navigateMock.mock.calls[0] as [
    string,
    { state: { fresh: unknown } },
  ];
  expect(path).toMatch(/^\/s\/claude\/[0-9a-f-]{36}$/);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
  // /a is Alpha's adopted folder → folder resolution already yields Alpha → no redundant stamp.
  expect(api.setSessionProject).not.toHaveBeenCalled();
});

test("changing the project switches the default folder (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await chooseProject(user, "p-b");
  expect(
    launchFolder(),
  ).toBe("/b");
});

test("'no project' launches in the config default folder without stamping (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"], { default_project: "/d" });
  await chooseProject(user, "");
  expect(
    launchFolder(),
  ).toBe("/d");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls[0] as [
    string,
    { state: { fresh: { cwd: string } } },
  ];
  expect(opts.state.fresh.cwd).toBe("/d");
  expect(api.setSessionProject).not.toHaveBeenCalled();
});

test("opencode stamping uses the new-<uuid> placeholder key (#448)", async () => {
  const user = userEvent.setup();
  // A folderless project launches in the legacy folder — a cwd no project adopts, so the explicit
  // project must be stamped (#448, #1458).
  mockEntities.mockResolvedValue({ projects: [FOLDERLESS] });
  renderLanding(["opencode"], { default_project: "/legacy" });
  await findProjects();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  const id = path.split("/").pop() as string;
  expect(id).toMatch(/^new-[0-9a-f-]{36}$/);
  expect(api.setSessionProject).toHaveBeenCalledWith(`opencode:${id}`, "p-x");
});

const DRAFT: NewSessionDraft = {
  engineChoice: "opencode",
  bypassChoice: false,
  returnTo: null,
  projectChoice: "p-b",
};

test("+ New project… saves the draft into this entry, then opens the wizard with it (#1187)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "opencode"]);
  await chooseProject(user, "");
  await chooseAgent(user, "opencode");
  await user.click(screen.getByRole("checkbox", { name: /skip permission/i }));
  await user.click(screen.getByRole("link", { name: /new project/i }));
  const draft = {
    engineChoice: "opencode",
    bypassChoice: false,
    returnTo: null,
    // The explicit "no project" survives as "", distinct from untouched null.
    projectChoice: "",
  };
  // First THIS entry learns the draft (so the browser's Back restores it), then the push.
  expect(navigateMock.mock.calls).toEqual([
    ["/", { replace: true, state: { restoreDraft: draft } }],
    [NEW_PROJECT_PATH, { state: { from: "new-session", draft } }],
  ]);
  expect(api.createProject).not.toHaveBeenCalled();
});

test("a modified click (new tab) is left to the browser — the link's own href (#1187)", async () => {
  renderLanding(["claude"]);
  await findProjects();
  const link = screen.getByRole("link", { name: /new project/i });
  expect(link).toHaveAttribute("href", NEW_PROJECT_PATH);
  link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
  expect(navigateMock).not.toHaveBeenCalled();
});

test("the draft records the map return, so the detour keeps it (#1187)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"], {}, { returnTo: MAP_PATH });
  await findProjects();
  await user.click(screen.getByRole("link", { name: /new project/i }));
  const [, [, pushed]] = navigateMock.mock.calls as [unknown, [string, { state: { draft: unknown } }]];
  expect(pushed.state.draft).toMatchObject({ returnTo: MAP_PATH, projectChoice: null });
  expect(navigateMock.mock.calls[0][1]).toMatchObject({ state: { returnTo: MAP_PATH } });
});

test("finish: the created project and its folder are selected, the agent/bypass choices restored (#1187)", async () => {
  renderLanding(
    ["claude", "opencode"],
    {},
    {
      returnTo: MAP_PATH,
      restoreDraft: { ...DRAFT, projectChoice: null },
      selectProjectId: "p-b",
    },
  );
  await findProjects();
  expect(projectValue()).toBe("p-b");
  expect(launchFolder()).toBe("/b");
  expect(agentValue()).toBe(
    "opencode",
  );
  expect(screen.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
  // One-shot: the restore is dropped from the history entry, the map return is kept.
  expect(navigateMock).toHaveBeenCalledWith("/", {
    replace: true,
    state: { returnTo: MAP_PATH },
  });
});

test("an old draft's folder override is ignored — the project's current folder wins (#1458)", async () => {
  const user = userEvent.setup();
  // Pre-#1458 drafts carried a `cwdOverride`. Invisible state must never pick the launch
  // directory: the folder follows the project, whatever the restored draft says.
  renderLanding(["claude"], {}, { restoreDraft: { ...DRAFT, cwdOverride: "/b/sub" } });
  await findProjects();
  expect(projectValue()).toBe("p-b");
  expect(launchFolder()).toBe("/b");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls.at(-1) as [
    string,
    { state: { fresh: { cwd: string } } },
  ];
  expect(opts.state.fresh.cwd).toBe("/b");
  expect(navigateMock).toHaveBeenCalledWith("/", { replace: true, state: null });
});

test("cancel keeps an explicit “no project” — it never falls back to the default (#1187)", async () => {
  renderLanding(["claude"], {}, { restoreDraft: { ...DRAFT, projectChoice: "" } });
  await findProjects();
  expect(projectValue()).toBe("");
});

test("cancel keeps an UNTOUCHED project choice untouched — the default selection applies (#1187)", async () => {
  renderLanding(
    ["claude"],
    { default_project_id: "p-b" },
    { restoreDraft: { ...DRAFT, projectChoice: null } },
  );
  await findProjects();
  expect(projectValue()).toBe("p-b");
});

test("malformed restore state is ignored, not trusted (#1187)", async () => {
  renderLanding(["claude"], {}, { restoreDraft: "nope", selectProjectId: 42 });
  await findProjects();
  expect(projectValue()).toBe("p-a");
  expect(navigateMock).not.toHaveBeenCalled();
});

test("with no entities the list offers only “No project”, and New project (#448, #1436)", async () => {
  mockEntities.mockResolvedValue({ projects: [] });
  renderLanding(["claude"]);
  const group = await findProjects();
  expect(within(group).getAllByRole("radio").map((r) => (r as HTMLInputElement).value)).toEqual([""]);
  expect(projectValue()).toBe("");
  expect(screen.getByRole("link", { name: /new project/i })).toBeInTheDocument();
});

// ---- Starred default project (#615 Phase 2) -------------------------------------------
//
// Before this pref, the pre-selected project was `entities[0]` — alphabetically first (the
// server sorts by name) and unsettable. `config.default_project_id` names it explicitly.
// `api.projectEntities()` is filtered to unarchived here, so an archived starred project is
// simply absent from `entities` — the same code path as a deleted one.

test("the starred project is pre-selected, not the alphabetically-first one (#615)", async () => {
  renderLanding(["claude"], { default_project_id: "p-b" });
  await findProjects();
  expect(projectValue()).toBe("p-b");
  expect(
    launchFolder(),
  ).toBe("/b");
});

test("a starred project that no longer exists falls back to the first project (#615)", async () => {
  // Deleted, or archived — `projectEntities()` drops archived, so both look like this.
  renderLanding(["claude"], { default_project_id: "p-gone" });
  await findProjects();
  expect(projectValue()).toBe("p-a");
  expect(
    launchFolder(),
  ).toBe("/a");
});

test("an archived starred project falls back — it is absent from the entity list (#615)", async () => {
  mockEntities.mockResolvedValue({ projects: [ENTITIES[1]] }); // only Beta survives the archive
  renderLanding(["claude"], { default_project_id: "p-a" });
  await findProjects();
  expect(projectValue()).toBe("p-b");
});

test("a starred project with no default folder falls through to the legacy cwd (#615)", async () => {
  // `default_folder: ""` is the #448 back-compat shape for a folderless project. "" is a MISSING
  // folder, not a chosen one — so `||`, not `??`, or the picker opens on nothing.
  mockEntities.mockResolvedValue({
    projects: [{ ...ENTITIES[0], default_folder: "" }, ENTITIES[1]],
  });
  renderLanding(["claude"], {
    default_project_id: "p-a",
    default_project: "/legacy",
  });
  await findProjects();
  expect(projectValue()).toBe("p-a"); // still starred…
  expect(
    launchFolder(),
  ).toBe("/legacy");
});

// ---- the stored agent defaults (#1128) --------------------------------------------------------------

const FIXTURE = fixture.engines as EngineInfo[];

/** The fixture with one API client's `api` block patched (#1339). */
function withApi(rows: EngineInfo[], id: string, patch: Partial<NonNullable<EngineInfo["api"]>>) {
  return rows.map((r) => (r.id === id && r.api ? { ...r, api: { ...r.api, ...patch } } : r));
}

async function startAndReadFresh(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /start session/i }));
  return navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
}

test("the form starts from the stored default agent and bypass (#1128)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "codex"], {
    agent_defaults: { default_engine: "codex", bypass: false },
  });
  await findProjects();
  expect(
    agentValue(),
  ).toBe("codex");
  expect(
    screen.getByRole("checkbox", { name: /skip permission prompts/i }),
  ).not.toBeChecked();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  const [path, opts] = await startAndReadFresh(user);
  expect(path).toMatch(/^\/s\/codex\//);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: false });
});

test("the operator can still change both for this session (#1128)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "codex"], {
    agent_defaults: { default_engine: "codex", bypass: false },
  });
  await findProjects();
  await chooseAgent(user, "claude");
  await user.click(
    screen.getByRole("checkbox", { name: /skip permission prompts/i }),
  );
  const [path, opts] = await startAndReadFresh(user);
  expect(path).toMatch(/^\/s\/claude\//);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
});

test("bypass stays on when the server sends no agent defaults (#1128)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
});

test("a default that is not installed falls back visibly, never silently (#1128)", async () => {
  setRoster(
    FIXTURE.map((e) =>
      e.id === "gemini" ? { ...e, present: false, bin: null } : e,
    ),
  );
  renderLanding(["claude", "codex"], {
    agent_defaults: { default_engine: "gemini", bypass: true },
  });
  await findProjects();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Your default, Gemini CLI, is not installed — new sessions use Claude Code until it is. Your choice is kept.",
  );
  expect(
    agentValue(),
  ).toBe("claude");
});

test("starting an API-agent conversation is single-flight — two clicks create ONE (Hermes on #1219)", async () => {
  const user = userEvent.setup();
  let resolve!: (v: { id: string }) => void;
  vi.mocked(api.chatNew).mockReset().mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  renderLanding(["apichat"], { default_project: "/d" });
  await chooseProject(user, "");
  const startBtn = screen.getByRole("button", { name: /start session/i });
  await user.click(startBtn);
  await user.click(startBtn);
  expect(api.chatNew).toHaveBeenCalledTimes(1);
  expect(startBtn).toBeDisabled();
  resolve({ id: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b" });
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
});

test("a chat creation that completes after the form is gone does not navigate (Hermes on #1219)", async () => {
  const user = userEvent.setup();
  let resolve!: (v: { id: string }) => void;
  vi.mocked(api.chatNew).mockReset().mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  const { unmount } = renderLanding(["apichat"], { default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  unmount();
  resolve({ id: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b" });
  await new Promise((r) => setTimeout(r, 0));
  expect(navigateMock).not.toHaveBeenCalled();
});

// ---- #1189: the model picker ---------------------------------------------------------------------

test("the model select starts on `default`, and `default` launches exactly as before (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  const model = screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
  expect(model.value).toBe("default");
  expect(model.options[0].value).toBe("default");
  // The roster's own list, aliases shown beside the id.
  expect(Array.from(model.options).map((o) => o.value)).toContain("claude-opus-5");
  expect(screen.getByRole("option", { name: "claude-opus-5 (opus)" })).toBeInTheDocument();
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
});

test("a chosen model rides the launch (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "claude-sonnet-5");
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true, model: "claude-sonnet-5" });
});

test("switching engine clears the model (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["claude", "codex"]);
  await findProjects();
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "claude-opus-5");
  await chooseAgent(user, "codex");
  expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe(
    "default",
  );
  // …and back: the claude choice does not come back either.
  await chooseAgent(user, "claude");
  expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe(
    "default",
  );
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
});

test("an engine whose model is configured elsewhere offers no select, and says why (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["opencode"]);
  await findProjects();
  expect(screen.queryByRole("combobox", { name: "Model" })).not.toBeInTheDocument();
  expect(screen.getByTestId("new-session-model-elsewhere")).toHaveTextContent(
    "own configuration",
  );
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true });
});

test("a restored choice the roster no longer offers is shown and SENT, never swapped for default (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const draft: NewSessionDraft = {
    engineChoice: "claude",
    bypassChoice: null,
    returnTo: null,
    projectChoice: null,
    modelChoice: { engine: "claude", model: "claude-retired-1" },
  };
  renderLanding(["claude"], {}, { restoreDraft: draft });
  await findProjects();
  const model = screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
  expect(model.value).toBe("claude-retired-1");
  expect(screen.getByRole("option", { name: /no longer offered/ })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  // calls[0] is the one-shot restore `replace`; the launch is the last navigation.
  const [, opts] = navigateMock.mock.calls.at(-1) as [string, { state: { fresh: unknown } }];
  // The server refuses it (4422); the form never quietly launches on `default` instead.
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true, model: "claude-retired-1" });
});

// ---- Native API clients (#1311) -------------------------------------------------------------------

test("agents are grouped Console vs API; an unavailable client is listed, disabled, with why", async () => {
  setRoster(FIXTURE);
  renderLanding(["claude", "codex", "codex-api"], {
    unavailable_clients: [
      { id: "claude-api", label: "Claude — API", reason: "claude 2.1.287 or later is required" },
    ],
  });
  const group = await screen.findByRole("radiogroup", { name: "Agent" });
  expect(within(group).getByText("Terminal")).toBeInTheDocument();
  expect(within(group).getByText("API — structured, no terminal")).toBeInTheDocument();
  // The unavailable client is a disabled tile whose reason is part of its accessible name — no
  // separate box (#1436), and never silently absent (#1311).
  const radios = within(group).getAllByRole("radio") as HTMLInputElement[];
  expect(radios.map((r) => [r.value, r.disabled])).toEqual([
    ["claude", false],
    ["codex", false],
    ["codex-api", false],
    ["unavailable:claude-api", true],
  ]);
  expect(
    within(group).getByRole("radio", { name: /claude — api.*claude 2\.1\.287 or later is required/i }),
  ).toBeDisabled();
  expect(screen.getByTestId("new-session-unavailable")).toHaveTextContent(
    "claude 2.1.287 or later is required",
  );
});

test("an API client's Skip permission prompts follows the stored default, only when its adapter maps it (#1473)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const view = renderLanding(["claude", "codex-api"], {
    default_project: "/d",
    agent_defaults: { default_engine: "claude", bypass: true },
  });
  // The console agent follows the stored default (on)…
  expect(await screen.findByRole("checkbox", { name: /skip permission prompts/i })).toBeChecked();
  await chooseAgent(user, "codex-api");
  // The old "API client · ready" box is gone (#1436): the tile names the CLI it drives.
  expect(screen.queryByTestId("new-session-api-about")).toBeNull();
  expect(screen.getByRole("radio", { name: /codex — api/i })).toHaveAccessibleName(/via codex cli/i);
  const skip = screen.getByRole("checkbox", { name: /skip permission prompts/i });
  // …and so does the API client now (operator decision on #1473, superseding #1339's
  // "unticked by default"), with the warning saying what that means.
  expect(skip).toBeChecked();
  expect(screen.getByTestId("api-bypass-warning")).toHaveTextContent("Fixed for this session");
  await user.click(skip);
  expect(screen.queryByTestId("api-bypass-warning")).toBeNull();
  view.unmount();
  // A stored default of OFF starts the API client guarded too.
  const off = renderLanding(["claude", "codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseAgent(user, "codex-api");
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  off.unmount();
  // A client whose adapter has not mapped it (the server says so) offers no toggle at all, and
  // never skips — whatever the stored default says.
  setRoster(withApi(FIXTURE, "codex-api", { can_bypass: false }));
  renderLanding(["claude", "codex-api"], { default_project: "/d", agent_defaults: { bypass: true } });
  await chooseAgent(user, "codex-api");
  expect(screen.queryByRole("checkbox", { name: /skip permission prompts/i })).toBeNull();
});

test("with the stored default on, an API create SKIPS: create, then start (#1473)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { default_project: "/d", agent_defaults: { bypass: true } });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(api.structuredStart).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(true);
  // A skip create keeps no client recovery slot (Hermes 5913) — unchanged by the new default.
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
});

test("a console skip choice never carries into an API client (Hermes on #1341)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["claude", "codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  const consoleSkip = screen.getByRole("checkbox", { name: /skip permission prompts/i });
  await user.click(consoleSkip); // explicitly on, for the console agent (the stored default is off)
  expect(consoleSkip).toBeChecked();
  await chooseAgent(user, "codex-api");
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  expect(screen.queryByTestId("api-bypass-warning")).toBeNull();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(false);
});

test("an API skip tick is never restored from a draft and never survives an agent change (#1339, 5913)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const draft: NewSessionDraft = {
    engineChoice: "codex-api",
    bypassChoice: true,
    returnTo: null,
    projectChoice: null,
  };
  renderLanding(["claude", "codex-api"], { ...GUARDED, default_project: "/d" }, { restoreDraft: draft });
  const skip = await screen.findByRole("checkbox", { name: /skip permission prompts/i });
  expect(skip).not.toBeChecked(); // a draft's (console) choice is not an API tick
  await user.click(skip);
  expect(skip).toBeChecked();
  // Leaving the client and coming back clears it: every agent change needs a fresh tick (5913).
  await chooseAgent(user, "claude");
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).toBeChecked(); // console: its own (restored) choice
  await chooseAgent(user, "codex-api");
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
});

test("Try again after the session limit asks the form as it is now: a spent Skip choice stays spent (#1819)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  let retry: (() => void) | undefined;
  const offRetry = listenSessionLimit((next) => {
    retry = next ?? undefined;
  });
  try {
    vi.mocked(api.structuredCreate)
      .mockReset()
      .mockRejectedValueOnce(new ApiError(429, "Session limit reached: 1 of 1 running."))
      .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
    renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
    await chooseProject(user, "");
    await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
    await user.click(screen.getByRole("button", { name: /start session/i }));
    await screen.findByTestId("start-error");
    expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(true);
    // A Skip choice covers one create: the form is back on its guarded default…
    expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
    // …so the retry the dialog holds must not send what the form no longer shows.
    await act(async () => retry?.());
    await vi.waitFor(() => expect(api.structuredCreate).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.structuredCreate).mock.calls[1][4]).toBe(false);
  } finally {
    offRetry();
    sessionStorage.clear();
  }
});

test("a lost skip create keeps no client slot: the error points at the session list, a reload asks nothing (5913)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate).mockReset().mockRejectedValueOnce(new TypeError("response lost"));
  const first = renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(await screen.findByTestId("start-error")).toHaveTextContent(
    "waits in your session list and runs only if you start it there",
  );
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
  first.unmount(); // the page reloads
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await findProjects();
  expect(api.structuredSnapshot).not.toHaveBeenCalled();
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
});

test("a lost skip create on the same form: the next create is a new, guarded one and leaves no slot (5913)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockRejectedValueOnce(new TypeError("response lost"));
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(api.structuredCreate).toHaveBeenCalledTimes(2));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(a[4]).toBe(true);
  expect(b[4]).toBe(false);
  expect(b[2]).not.toBe(a[2]);
  // Only the guarded one is held for a same-form retry; the skip one is the server's to list.
  expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe(b[2]);
  sessionStorage.clear();
});

test("a lost skip create the server never made is forgotten: the next start is guarded, with a new id (#1339)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const first = renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  first.unmount();
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" }); // the lookup 404s (beforeEach)
  await chooseProject(user, "");
  await vi.waitFor(() =>
    expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull(),
  );
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(a[4]).toBe(true);
  expect(b[4]).toBe(false); // a skip is only ever sent by a fresh tick
  expect(b[2]).not.toBe(a[2]);
});

test("a fresh API form is still guarded when the pending create was guarded (#1339)", async () => {
  setRoster(FIXTURE);
  sessionStorage.setItem(
    "battlelab.pendingStructuredCreate",
    JSON.stringify({ engine: "codex-api", cwd: "/d", id: "x", bypass: false }),
  );
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  expect(await screen.findByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  sessionStorage.clear();
});

test.each([
  ["succeeds", "ok"],
  ["is refused", "refused"],
])("a later skip create that %s never clears a lost guarded create's slot (Hermes 5916)", async (_, outcome) => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost")); // the guarded one: committed, response lost
  if (outcome === "ok") {
    vi.mocked(api.structuredCreate).mockResolvedValueOnce({
      session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f",
    } as never);
  } else {
    vi.mocked(api.structuredCreate).mockRejectedValueOnce(new ApiError(422, "refused"));
  }
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i })); // guarded
  await screen.findByTestId("start-error");
  const guarded = JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!) as { id: string };
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i })); // a different, skip create
  await vi.waitFor(() => expect(api.structuredCreate).toHaveBeenCalledTimes(2));
  await vi.waitFor(() =>
    expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe(guarded.id),
  );
  sessionStorage.clear();
});

test("a found create stays recoverable until it is OPENED — a late or unmounted lookup erases nothing (Hermes 5920)", async () => {
  setRoster(FIXTURE);
  sessionStorage.setItem(
    "battlelab.pendingStructuredCreate",
    JSON.stringify({ engine: "codex-api", cwd: "/d", id: "made-id" }),
  );
  let release!: () => void;
  vi.mocked(api.structuredSnapshot)
    .mockReset()
    .mockImplementationOnce(() => new Promise((r) => (release = () => r({} as never))))
    .mockResolvedValue({} as never);
  const first = renderLanding(["codex-api"], { default_project: "/d" });
  await findProjects();
  first.unmount(); // navigated away before the lookup answered
  release();
  await new Promise((r) => setTimeout(r, 0));
  expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe("made-id");
  const user = userEvent.setup();
  renderLanding(["codex-api"], { default_project: "/d" }); // back to the form: offered again
  const link = await screen.findByRole("link", { name: "Open previous session" });
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).not.toBeNull();
  await user.click(link);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
});

test.each(["opencode-api", "codex-api", "claude-api"])("a recovered %s attempt appears only for its selected API client (#1373)", async (engine) => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const saved = JSON.stringify({ engine, cwd: "/d", id: "previous-id" });
  sessionStorage.setItem("battlelab.pendingStructuredCreate", saved);
  vi.mocked(api.structuredSnapshot).mockResolvedValue({ native: null } as never);
  vi.mocked(api.structuredCreate).mockReset();
  renderLanding([engine, "opencode", "apichat", engine === "codex-api" ? "claude-api" : "codex-api"]);
  await chooseAgent(user, engine);
  await screen.findByTestId("api-recovered-create");
  for (const choice of ["opencode", "apichat", engine === "codex-api" ? "claude-api" : "codex-api"]) {
    await chooseAgent(user, choice);
    expect(screen.queryByTestId("api-recovered-create")).toBeNull();
    expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(saved);
  }
  await chooseAgent(user, engine);
  expect(await screen.findByTestId("api-recovered-create")).toHaveTextContent("Open it to check its status");
  expect(api.structuredCreate).not.toHaveBeenCalled();
  expect(api.structuredStart).not.toHaveBeenCalled();
  sessionStorage.clear();
});

test("dismissing a found attempt permits a fresh guarded create after reload (#1373)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  sessionStorage.setItem("battlelab.pendingStructuredCreate", JSON.stringify({ engine: "opencode-api", cwd: "/d", id: "previous-id" }));
  vi.mocked(api.structuredSnapshot).mockResolvedValue({ native: null } as never);
  vi.mocked(api.structuredCreate).mockReset().mockRejectedValue(new TypeError("response lost"));
  const first = renderLanding(["opencode-api"], { ...GUARDED, default_project: "/d" });
  await user.click(await screen.findByRole("button", { name: "Dismiss reminder" }));
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
  expect(api.structuredCreate).not.toHaveBeenCalled();
  expect(api.structuredStart).not.toHaveBeenCalled();
  first.unmount();
  renderLanding(["opencode-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  expect(api.structuredSnapshot).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  const args = vi.mocked(api.structuredCreate).mock.calls[0];
  expect(args[2]).not.toBe("previous-id");
  expect(args[4]).toBe(false);
  // A lost response for the NEW request must still retain its own retry identity.
  expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe(args[2]);
  sessionStorage.clear();
});

test("dismissing an older notice never clears a newer stored attempt (#1373)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  sessionStorage.setItem("battlelab.pendingStructuredCreate", JSON.stringify({ engine: "codex-api", cwd: "/d", id: "older-id" }));
  vi.mocked(api.structuredSnapshot).mockResolvedValue({} as never);
  renderLanding(["codex-api"]);
  const dismiss = await screen.findByRole("button", { name: "Dismiss reminder" });
  const newer = JSON.stringify({ engine: "opencode-api", cwd: "/new", id: "newer-id" });
  sessionStorage.setItem("battlelab.pendingStructuredCreate", newer);
  await user.click(dismiss);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(newer);
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  sessionStorage.clear();
});

test("a recovered attempt cannot be dismissed while its guarded retry is in flight (#1373)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const saved = JSON.stringify({ engine: "codex-api", cwd: "/d", id: "previous-id" });
  sessionStorage.setItem("battlelab.pendingStructuredCreate", saved);
  vi.mocked(api.structuredSnapshot).mockResolvedValue({} as never);
  let reject!: (reason: unknown) => void;
  vi.mocked(api.structuredCreate).mockReset().mockImplementation(() => new Promise((_, r) => { reject = r; }));
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  const dismiss = await screen.findByRole("button", { name: "Dismiss reminder" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(dismiss).toBeDisabled();
  await user.click(dismiss);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(saved);
  expect(vi.mocked(api.structuredCreate).mock.calls[0][2]).toBe("previous-id");
  reject(new TypeError("response lost"));
  await screen.findByTestId("start-error");
  expect(dismiss).toBeEnabled();
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(saved);
  sessionStorage.clear();
});

test("a newer create hides the older reminder while the form stays mounted (#1373)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  sessionStorage.setItem("battlelab.pendingStructuredCreate", JSON.stringify({ engine: "codex-api", cwd: "/old", id: "previous-id" }));
  vi.mocked(api.structuredSnapshot).mockResolvedValue({} as never);
  let reject!: (reason: unknown) => void;
  vi.mocked(api.structuredCreate).mockReset().mockImplementation(() => new Promise((_, r) => { reject = r; }));
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/new" });
  await screen.findByTestId("api-recovered-create");
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  const nextId = vi.mocked(api.structuredCreate).mock.calls[0][2];
  expect(nextId).not.toBe("previous-id");
  reject(new TypeError("response lost"));
  await screen.findByTestId("start-error");
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe(nextId);
  sessionStorage.clear();
});

test("opening a recovered session during its retry preserves the identity if the response is lost (#1373)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  const saved = JSON.stringify({ engine: "opencode-api", cwd: "/d", id: "previous-id" });
  sessionStorage.setItem("battlelab.pendingStructuredCreate", saved);
  vi.mocked(api.structuredSnapshot).mockResolvedValue({} as never);
  let reject!: (reason: unknown) => void;
  vi.mocked(api.structuredCreate).mockReset()
    .mockImplementationOnce(() => new Promise((_, r) => { reject = r; }))
    .mockRejectedValue(new TypeError("response lost again"));
  const first = renderLanding(["opencode-api"], { ...GUARDED, default_project: "/d" });
  const open = await screen.findByRole("link", { name: "Open previous session" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await user.click(open);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(saved);
  reject(new TypeError("response lost"));
  await new Promise((r) => setTimeout(r, 0));
  first.unmount();
  renderLanding(["opencode-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  expect(vi.mocked(api.structuredCreate).mock.calls.map((args) => args[2])).toEqual(["previous-id", "previous-id"]);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(saved);
  sessionStorage.clear();
});

test.each([
  ["engine", "opencode-api"], ["id", "new-id"], ["cwd", "/new"], ["model", "other"], ["bypass", true],
] as const)("a late lookup cannot offer a stored attempt whose %s changed (#1373)", async (field, value) => {
  setRoster(FIXTURE);
  const saved = { engine: "codex-api", cwd: "/d", id: "previous-id", model: "default", bypass: false };
  sessionStorage.setItem("battlelab.pendingStructuredCreate", JSON.stringify(saved));
  let release!: () => void;
  vi.mocked(api.structuredSnapshot).mockImplementationOnce(() => new Promise((r) => { release = () => r({} as never); }));
  renderLanding(["codex-api"]);
  await findProjects();
  const changed = JSON.stringify({ ...saved, [field]: value });
  sessionStorage.setItem("battlelab.pendingStructuredCreate", changed);
  release();
  await new Promise((r) => setTimeout(r, 0));
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBe(changed);
  sessionStorage.clear();
});

test("a skip create is launched only after its response arrived: create, THEN start (#1339)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  const order: string[] = [];
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockImplementationOnce(async () => {
      order.push("create");
      return { session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never;
    });
  vi.mocked(api.structuredStart).mockImplementationOnce(async () => {
    order.push("start");
    return {} as never;
  });
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(order).toEqual(["create", "start"]);
  expect(api.structuredStart).toHaveBeenCalledWith("codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f");
});

test("a guarded create never calls start (#1339)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(api.structuredStart).not.toHaveBeenCalled();
});

test("one tick authorizes one create: after a lost skip attempt the next create is guarded (Hermes on #1341, 5876)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "gpt-6-luna"); // a different create
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(a[4]).toBe(true);
  expect(b[4]).toBe(false);
  expect(b[2]).not.toBe(a[2]);
});

test.each([
  ["found", () => Promise.resolve({} as never)],
  ["404", () => Promise.reject(new ApiError(404, "no such native session"))],
])("an older recovery lookup (%s) never clears a newer create's id (Hermes on #1341, 5876)", async (_, settle) => {
  setRoster(FIXTURE);
  sessionStorage.setItem(
    "battlelab.pendingStructuredCreate",
    JSON.stringify({ engine: "codex-api", cwd: "/old", id: "old-id" }),
  );
  let release!: () => void;
  vi.mocked(api.structuredSnapshot)
    .mockReset()
    .mockImplementationOnce(() => new Promise((r) => (release = () => r(undefined as never))).then(settle));
  vi.mocked(api.structuredCreate).mockReset().mockRejectedValueOnce(new TypeError("response lost"));
  const user = userEvent.setup();
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i })); // a NEW, guarded create
  await screen.findByTestId("start-error");
  const newer = JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!) as { id: string };
  expect(newer.id).not.toBe("old-id");
  release(); // the old lookup settles late
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  expect(JSON.parse(sessionStorage.getItem("battlelab.pendingStructuredCreate")!).id).toBe(newer.id);
  expect(screen.queryByTestId("api-recovered-create")).toBeNull();
  sessionStorage.clear();
});

test("a retry reuses the id only for the same mode: switching it is a new create (#1339)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  await user.click(screen.getByRole("checkbox", { name: /skip permission prompts/i }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(a[4]).toBe(false);
  expect(b[4]).toBe(true);
  expect(b[2]).not.toBe(a[2]); // never resend an id with the other mode
});

test("starting an API client creates it on the server — single-flight, retry reuses the id", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("network"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  const startBtn = screen.getByRole("button", { name: /start session/i });
  await user.click(startBtn);
  await screen.findByTestId("start-error");
  await user.click(startBtn);
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const calls = vi.mocked(api.structuredCreate).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(["codex-api", "/d", calls[0][2], "default", false]); // guarded by default (#1339)
  expect(calls[1][2]).toBe(calls[0][2]); // the same create, never a second session
  expect(navigateMock).toHaveBeenCalledWith("/s/codex-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f");
});

test("an unresolved API create survives a reload: Start after remount reuses the same id", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const first = renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  first.unmount(); // the page reloads
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(b[2]).toBe(a[2]);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
});

test("an API client offers the models its own CLI reports and creates on the chosen one (#1313)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  const select = await screen.findByRole("combobox", { name: "Model" });
  expect(api.structuredModels).toHaveBeenCalledWith("codex-api");
  expect(Array.from(select.querySelectorAll("option")).map((o) => [o.value, o.textContent])).toEqual([
    ["default", "default"],
    ["gpt-6.1-sol", "GPT-6.1 Sol (gpt-6.1-sol)"],
    ["gpt-6-luna", "gpt-6-luna"],
  ]);
  await user.selectOptions(select, "gpt-6-luna");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [call] = vi.mocked(api.structuredCreate).mock.calls;
  expect(call).toEqual(["codex-api", "/d", call[2], "gpt-6-luna", false]); // + guarded (#1339)
});

test("an API client whose CLI cannot list models says why and offers default only", async () => {
  setRoster(FIXTURE);
  vi.mocked(api.structuredModels).mockResolvedValue({
    status: "unavailable",
    models: [],
    reason: "the agent did not answer in time",
  });
  renderLanding(["codex-api"], { default_project: "/d" });
  expect(await screen.findByTestId("new-session-model-api")).toHaveTextContent(
    "Models unavailable — the agent did not answer in time. Only default can start.",
  );
  expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();
});

test("choosing another model after a lost create is a new create, never the same operation id", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { default_project: "/d" });
  await chooseProject(user, "");
  const select = await screen.findByRole("combobox", { name: "Model" });
  await user.selectOptions(select, "gpt-6.1-sol");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  await user.selectOptions(select, "gpt-6-luna");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(a[3]).toBe("gpt-6.1-sol");
  expect(b[3]).toBe("gpt-6-luna");
  expect(b[2]).not.toBe(a[2]);
});

test("a chosen API model whose list then fails keeps a select: it shows the stale choice and default still starts", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  vi.mocked(api.structuredModels).mockResolvedValue({
    status: "unavailable",
    models: [],
    reason: "the agent did not answer in time",
  });
  // A restored draft carries the model chosen before the list became unreadable.
  const draft: NewSessionDraft = {
    engineChoice: "codex-api",
    bypassChoice: null,
    returnTo: null,
    projectChoice: "",
    modelChoice: { engine: "codex-api", model: "gpt-6-luna" },
  };
  renderLanding(["codex-api"], { default_project: "/d" }, { restoreDraft: draft });
  await screen.findByTestId("new-session-model-api");
  const select = screen.getByRole("combobox", { name: "Model" });
  expect(select).toHaveValue("gpt-6-luna");
  expect(screen.getByRole("option", { name: "gpt-6-luna (no longer offered)" })).toBeInTheDocument();
  await user.selectOptions(select, "default");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  // calls[0] is the one-shot restore `replace`; the launch is the second navigation.
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][3]).toBe("default");
});

// ---- #1407: API new-session defaults preselect; effort is part of the create ----------------------

const LEVELS = {
  status: "ok" as const,
  reason: null,
  models: [
    { id: "gpt-6.1-sol", label: "GPT-6.1 Sol", description: null, efforts: ["low", "high"], is_default: true },
    { id: "gpt-6-luna", label: "gpt-6-luna", description: null, efforts: [], is_default: false },
  ],
};
const DEFAULTS = (model: string, effort: string | null) => ({
  agent_defaults: {
    default_engine: null,
    bypass: false, // these pin model/effort on the GUARDED create
    session_defaults: { "codex-api": { model, effort } },
  },
  default_project: "/d",
});

test("the stored defaults preselect model and effort, and the create sends them (#1407)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  await chooseProject(user, "");
  await vi.waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6.1-sol"),
  );
  expect(screen.getByTestId("new-session-effort")).toHaveValue("high");
  expect(screen.getByTestId("new-session-defaults")).toHaveTextContent(/From your .* defaults/);
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [call] = vi.mocked(api.structuredCreate).mock.calls;
  expect(call).toEqual(["codex-api", "/d", call[2], "gpt-6.1-sol", false, "high"]);
});

test("changing the level is for this session only, and Reset restores the defaults", async () => {
  setRoster(FIXTURE);
  vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
  const user = userEvent.setup();
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  const effort = await screen.findByTestId("new-session-effort");
  await user.selectOptions(effort, "low");
  expect(screen.getByTestId("new-session-defaults")).toHaveTextContent(
    "Changed for this session — your defaults are gpt-6.1-sol · high.",
  );
  await user.click(screen.getByRole("button", { name: "Reset to defaults" }));
  expect(screen.getByTestId("new-session-effort")).toHaveValue("high");
});

test("a model without levels shows no effort select; switching back restores the preset level", async () => {
  setRoster(FIXTURE);
  vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
  const user = userEvent.setup();
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  const model = await screen.findByRole("combobox", { name: "Model" });
  await vi.waitFor(() => expect(model).toHaveValue("gpt-6.1-sol"));
  await user.selectOptions(model, "gpt-6-luna");
  expect(screen.queryByTestId("new-session-effort")).toBeNull();
  await user.selectOptions(model, "gpt-6.1-sol");
  expect(screen.getByTestId("new-session-effort")).toHaveValue("high");
});

test("a stored model the client dropped is named, not preselected", async () => {
  setRoster(FIXTURE);
  vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
  renderLanding(["codex-api"], DEFAULTS("gpt-5-gone", "high"));
  expect(await screen.findByTestId("new-session-defaults")).toHaveTextContent(
    /Your default model gpt-5-gone is no longer offered/,
  );
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("default");
});

test("choosing another level after a lost create is a new create, never the same operation id", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  await chooseProject(user, "");
  const effort = await screen.findByTestId("new-session-effort");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  await user.selectOptions(effort, "low");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect([a[5], b[5]]).toEqual(["high", "low"]);
  expect(b[2]).not.toBe(a[2]);
});

test("true cold with a saved default: Start is held while the saved model is checked, then enabled (#1407, #1503)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  let answer: (v: typeof LEVELS) => void = () => {};
  vi.mocked(api.structuredModels).mockReturnValue(new Promise((res) => (answer = res)));
  const user = userEvent.setup();
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  await chooseProject(user, "");
  // State (d): no list has confirmed the saved model yet — said, and Start waits.
  expect(await screen.findByTestId("new-session-model-checking")).toHaveTextContent(
    "Checking your saved model gpt-6.1-sol… or pick one to start now.",
  );
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent("Checking your saved model…");
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6.1-sol");
  expect(screen.getByRole("option", { name: "gpt-6.1-sol (checking…)" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  await act(async () => answer(LEVELS));
  await vi.waitFor(() => expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled());
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6.1-sol");
  expect(screen.getByTestId("new-session-effort")).toHaveValue("high");
  expect(screen.queryByTestId("new-session-model-checking")).toBeNull();
});

// ---- #1503: model lists are served from cache and refreshed behind it -------------------------

test("a hung list never traps the form: picking default ends the wait and starts on default (#1503 PR review)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  vi.mocked(api.structuredModels).mockReturnValue(new Promise(() => {})); // never answers
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const user = userEvent.setup();
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  await chooseProject(user, "");
  const select = await screen.findByRole("combobox", { name: "Model" });
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  await user.selectOptions(select, "default"); // an explicit pick — default needs no list
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
  expect(screen.queryByTestId("new-session-model-checking")).toBeNull();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [call] = vi.mocked(api.structuredCreate).mock.calls;
  expect(call).toEqual(["codex-api", "/d", call[2], "default", false]);
});

/** A request that never answers — the revalidation behind a cached list, still running. */
const hang = () => new Promise<never>(() => {});
/** Put a list in the store as an earlier visit (or the prefetch) would have. */
async function cacheList(engine: string, list: typeof LEVELS & Record<string, unknown>) {
  vi.mocked(api.structuredModels).mockResolvedValueOnce(list);
  await structuredModelsStore.revalidate(engine);
}

test("a cached list paints at once: no “asking…” hint, and Start is not held by the refresh (#1503)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  await cacheList("codex-api", LEVELS);
  vi.mocked(api.structuredModels).mockImplementation(hang);
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const user = userEvent.setup();
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  // The first paint already has the saved model and its level, from the cache.
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6.1-sol");
  expect(screen.getByTestId("new-session-effort")).toHaveValue("high");
  expect(screen.queryByTestId("new-session-model-loading")).toBeNull();
  expect(screen.queryByTestId("new-session-model-checking")).toBeNull();
  await chooseProject(user, "");
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [call] = vi.mocked(api.structuredCreate).mock.calls;
  expect(call).toEqual(["codex-api", "/d", call[2], "gpt-6.1-sol", false, "high"]);
});

test("true cold without a saved default: “asking…”, Start enabled, and it sends default because nothing was chosen (#1503)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  vi.mocked(api.structuredModels).mockImplementation(hang);
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const user = userEvent.setup();
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  expect(screen.getByTestId("new-session-model-loading")).toHaveTextContent(
    "Model: asking the agent which models it offers…",
  );
  expect(screen.queryByTestId("new-session-effort")).toBeNull();
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][3]).toBe("default");
});

test("a saved default the fresh list dropped gets the “no longer offered” notice, never a silent swap (#1503)", async () => {
  setRoster(FIXTURE);
  await cacheList("codex-api", LEVELS); // the cached list still has gpt-6.1-sol…
  const fresh = { ...LEVELS, models: LEVELS.models.filter((m) => m.id !== "gpt-6.1-sol") };
  vi.mocked(api.structuredModels).mockResolvedValue(fresh); // …the revalidation does not
  renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6.1-sol");
  expect(await screen.findByTestId("new-session-defaults")).toHaveTextContent(
    /Your default model gpt-6.1-sol is no longer offered/,
  );
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("default");
});

test("a create the server refuses (a CLI upgrade dropped the id) shows its reason and re-asks the list (#1503)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  await cacheList("codex-api", LEVELS);
  vi.mocked(api.structuredModels).mockClear().mockResolvedValue(LEVELS);
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValue(new ApiError(422, "codex-api: model 'gpt-6-luna' is not offered"));
  const user = userEvent.setup();
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "gpt-6-luna");
  const calls = vi.mocked(api.structuredModels).mock.calls.length;
  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(await screen.findByTestId("start-error")).toHaveTextContent(
    "codex-api: model 'gpt-6-luna' is not offered",
  );
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-6-luna"); // not swapped
  await vi.waitFor(() =>
    expect(vi.mocked(api.structuredModels).mock.calls.length).toBeGreaterThan(calls),
  );
});

test("a list the server is refreshing says “refreshing…” beside Model, and Start stays enabled (#1503)", async () => {
  setRoster(FIXTURE);
  await cacheList("codex-api", { ...LEVELS, stale: true, fetched_age_s: 640 });
  vi.mocked(api.structuredModels).mockImplementation(hang);
  const user = userEvent.setup();
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  expect(screen.getByTestId("new-session-model-refreshing")).toHaveTextContent("refreshing…");
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
  expect(screen.queryByTestId("new-session-model-refresh-failed")).toBeNull();
});

test("a failed refresh keeps the last list and says how old it is (#1503)", async () => {
  setRoster(FIXTURE);
  vi.mocked(api.structuredModels).mockResolvedValue({
    ...LEVELS,
    stale: true,
    fetched_age_s: 720,
    refresh_error: "the agent did not answer in time",
  });
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  expect(await screen.findByTestId("new-session-model-refresh-failed")).toHaveTextContent(
    "Couldn’t refresh the list — showing the last one (12 min old).",
  );
  expect(screen.getByRole("combobox", { name: "Model" })).toBeInTheDocument();
  expect(screen.queryByTestId("new-session-model-refreshing")).toBeNull();
});

test("every API client's list is asked for on arrival, so switching between them paints at once (#1503)", async () => {
  setRoster(FIXTURE);
  const claudeList = {
    ...LEVELS,
    models: [{ id: "opus", label: "Opus", description: null, efforts: [], is_default: false }],
  };
  vi.mocked(api.structuredModels).mockImplementation(async (engine: string) =>
    engine === "claude-api" ? claudeList : LEVELS,
  );
  const user = userEvent.setup();
  renderLanding(["codex-api", "claude-api"], { ...GUARDED, default_project: "/d" });
  await vi.waitFor(() =>
    expect(vi.mocked(api.structuredModels).mock.calls.map((c) => c[0]).sort()).toEqual(
      expect.arrayContaining(["claude-api", "codex-api"]),
    ),
  );
  await screen.findByRole("combobox", { name: "Model" });
  vi.mocked(api.structuredModels).mockImplementation(hang); // the switch's revalidation hangs
  await chooseAgent(user, "claude-api");
  expect(screen.queryByTestId("new-session-model-loading")).toBeNull();
  expect(screen.getByRole("option", { name: "Opus (opus)" })).toBeInTheDocument();
});

// ---- #1436: the polished form — cached projects, start-time freshness, folder contract --------

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("a cached project list paints at once; Start waits for this visit's revalidation (#1436)", async () => {
  projectPickerStore.commit(0, ENTITIES);
  const pending = deferred<{ projects: typeof ENTITIES }>();
  mockEntities.mockReturnValue(pending.promise);
  renderLanding(["claude"]);
  // No skeleton: the cached rows are there on the first render.
  expect(screen.queryByTestId("project-skeleton")).toBeNull();
  expect(projectValue()).toBe("p-a");
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent("Checking projects…");
  await act(async () => pending.resolve({ projects: ENTITIES }));
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent("Start");
});

test("a first-ever load shows fixed-height skeleton rows, then the list (#1436)", async () => {
  const pending = deferred<{ projects: typeof ENTITIES }>();
  mockEntities.mockReturnValue(pending.promise);
  renderLanding(["claude"]);
  expect(screen.getAllByTestId("project-skeleton")).toHaveLength(2);
  await act(async () => pending.resolve({ projects: ENTITIES }));
  expect(screen.queryByTestId("project-skeleton")).toBeNull();
  expect(projectValue()).toBe("p-a");
});

test("an explicit choice the fresh list dropped falls back BEFORE Start, and is never stamped (#1436)", async () => {
  const user = userEvent.setup();
  projectPickerStore.commit(0, ENTITIES); // the cache still has Beta…
  const pending = deferred<{ projects: typeof ENTITIES }>();
  mockEntities.mockReturnValue(pending.promise);
  renderLanding(["claude"], {}, { restoreDraft: { ...DRAFT, engineChoice: "claude" } });
  expect(projectValue()).toBe("p-b");
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  // …but Beta was archived meanwhile: the confirmed list has only Alpha.
  await act(async () => pending.resolve({ projects: [ENTITIES[0]] }));
  expect(projectValue()).toBe("p-a");
  expect(launchFolder()).toBe("/a");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls.at(-1) as [string, { state: { fresh: { cwd: string } } }];
  expect(opts.state.fresh.cwd).toBe("/a");
  expect(api.setSessionProject).not.toHaveBeenCalledWith(expect.anything(), "p-b");
});

test("a failed refresh keeps the cached list visible, and Start still works (#1436)", async () => {
  const user = userEvent.setup();
  projectPickerStore.commit(0, ENTITIES);
  mockEntities.mockRejectedValue(new ApiError(502, "bad gateway"));
  renderLanding(["claude"], {}, { restoreDraft: { ...DRAFT, engineChoice: "claude" } });
  expect(await screen.findByText(/couldn’t refresh your projects/i)).toBeInTheDocument();
  // An unreachable server proves nothing: the explicit choice stands, the rows stay.
  expect(projectValue()).toBe("p-b");
  const start = screen.getByRole("button", { name: /start session/i });
  expect(start).toBeEnabled();
  await user.click(start);
  expect(navigateMock).toHaveBeenCalled();
});

test("a failed first load says so and still lets the operator start without a project (#1436)", async () => {
  const user = userEvent.setup();
  mockEntities.mockRejectedValue(new ApiError(502, "bad gateway"));
  renderLanding(["claude"], { default_project: "/d" });
  expect(await screen.findByText(/couldn’t load your projects/i)).toBeInTheDocument();
  expect(projectValue()).toBe("");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(api.setSessionProject).not.toHaveBeenCalled();
});

const FOLDERLESS = { ...ENTITIES[0], id: "p-x", name: "Scratch", folders: [], default_folder: "" };

test("a folderless project with a global default starts there, and the summary says so (#1436, #1458)", async () => {
  mockEntities.mockResolvedValue({ projects: [FOLDERLESS] });
  renderLanding(["claude"], { default_project: "/legacy" });
  await findProjects();
  expect(launchFolder()).toBe("/legacy");
  // The "using your global default" note (#1436) moved into the summary (#1458).
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent(
    /Start Claude Code in \/legacyproject Scratch · using your global default folder/,
  );
  expect(screen.queryByText(/set one in/i)).toBeNull();
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("a folderless project with no global default points at Settings → Projects, Start disabled (#1458)", async () => {
  mockEntities.mockResolvedValue({ projects: [FOLDERLESS] });
  renderLanding(["claude"]);
  await findProjects();
  expect(launchFolder()).toBe("");
  const summary = screen.getByTestId("new-session-summary");
  expect(summary).toHaveTextContent(
    "“Scratch” has no default folder — set one in Settings → Projects.",
  );
  // The action is a real link to the projects settings: the folder is configured on the project,
  // never picked on this form (#1458).
  expect(
    within(summary).getByRole("link", { name: /settings → projects/i }),
  ).toHaveAttribute("href", settingsPath("projects"));
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
});

test("with no folder at all — no project, no global default — the summary says what to do (#1458)", async () => {
  mockEntities.mockResolvedValue({ projects: [FOLDERLESS] });
  renderLanding(["claude"]);
  await findProjects();
  await chooseProject(userEvent.setup(), "");
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent(
    /^Pick a project to start in\.$/,
  );
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
});

test("the form defines no folder — no Folder section, no picker, no override (#1458)", async () => {
  renderLanding(["claude"]);
  await findProjects();
  expect(screen.queryByRole("heading", { name: "Folder" })).toBeNull();
  expect(screen.queryByTestId("launch-folder")).toBeNull();
  expect(screen.queryByRole("button", { name: /choose folder/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /use project default/i })).toBeNull();
  expect(launchFolder()).toBe("/a");
});

test("more than six projects get a filter, and / focuses it (#1436)", async () => {
  const user = userEvent.setup();
  const many = Array.from({ length: 7 }, (_, i) => ({
    ...ENTITIES[0],
    id: `p-${i}`,
    name: `Project ${i}`,
    folders: [`/p${i}`],
    default_folder: `/p${i}`,
  }));
  mockEntities.mockResolvedValue({ projects: many });
  renderLanding(["claude"]);
  await findProjects();
  const filter = screen.getByRole("searchbox", { name: "Filter projects" });
  // From the body (nothing focused) — the state on arrival (#1437 review, round 2).
  await user.keyboard("/");
  expect(filter).toHaveFocus();
  act(() => filter.blur());
  // From a project row's radio — where focus sits after a click; a radio is not typing.
  await user.click(radioIn(screen.getByRole("radiogroup", { name: "Project" }), "p-3")!);
  await user.keyboard("/");
  expect(filter).toHaveFocus();
  await user.keyboard("6");
  const values = within(screen.getByRole("radiogroup", { name: "Project" }))
    .getAllByRole("radio")
    .map((r) => (r as HTMLInputElement).value);
  // The selected project always stays listed, so the filter never hides what Start would use.
  expect(values).toEqual(["", "p-3", "p-6"]);
});

test("Ctrl+Enter starts the session; the summary says what Start will do (#1436)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent(/Start Claude Code in \/a/);
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent("project Alpha");
  focusForm();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).toHaveBeenCalledWith(
    expect.stringMatching(/^\/s\/claude\//),
    expect.objectContaining({ state: expect.objectContaining({ fresh: expect.objectContaining({ cwd: "/a" }) }) }),
  );
});

test("Ctrl+Enter does nothing while Start is held (#1436)", async () => {
  const user = userEvent.setup();
  projectPickerStore.commit(0, ENTITIES);
  mockEntities.mockReturnValue(new Promise(() => {}));
  renderLanding(["claude"]);
  focusForm();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).not.toHaveBeenCalled();
});

test("the agent tiles mark the stored default and pick by click (#1436)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "codex"], { agent_defaults: { default_engine: "codex", bypass: true } });
  const group = await screen.findByRole("radiogroup", { name: "Agent" });
  expect(agentValue()).toBe("codex");
  expect(within(group).getByRole("radio", { name: /codex.*default/i })).toBeChecked();
  await chooseAgent(user, "claude");
  expect(agentValue()).toBe("claude");
});

test("a failed refresh never launches a choice it cannot vouch for — no folder, no stamp (Hermes on #1437)", async () => {
  const user = userEvent.setup();
  projectPickerStore.commit(0, [ENTITIES[0]]); // a cache that does not have p-gone
  mockEntities.mockRejectedValue(new ApiError(502, "bad gateway"));
  renderLanding(["claude"], { default_project: "/d" }, { restoreDraft: { ...DRAFT, engineChoice: "claude", projectChoice: "p-gone" } });
  expect(await screen.findByText(/couldn’t refresh your projects/i)).toBeInTheDocument();
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent(/couldn’t confirm the project you chose/i);
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  focusForm();
  await user.keyboard("{Control>}{Enter}{/Control}");
  // (The one navigate here is the restore's own history clean-up — never a launch.)
  expect(navigateMock).not.toHaveBeenCalledWith(expect.stringMatching(/^\/s\//), expect.anything());
  // Choosing again is the way out.
  await chooseProject(user, "p-a");
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("a project the wizard just created launches in ITS folder even when the refresh fails (Hermes on #1437)", async () => {
  const user = userEvent.setup();
  projectPickerStore.commit(0, [ENTITIES[0]]);
  // What `api.createProject` does on success: the created entity joins the picker's cache.
  projectPickerStore.add({ ...ENTITIES[1], id: "p-new", name: "Gamma", folders: ["/g"], default_folder: "/g" });
  mockEntities.mockRejectedValue(new ApiError(502, "bad gateway"));
  renderLanding(["claude"], { default_project: "/d" }, { selectProjectId: "p-new" });
  expect(await screen.findByText(/couldn’t refresh your projects/i)).toBeInTheDocument();
  expect(projectValue()).toBe("p-new");
  expect(launchFolder()).toBe("/g");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls.at(-1) as [string, { state: { fresh: { cwd: string } } }];
  expect(opts.state.fresh.cwd).toBe("/g");
});

test("a first visit holds Start until the projects arrive, even with a global folder (Hermes on #1437)", async () => {
  const user = userEvent.setup();
  const pending = deferred<{ projects: typeof ENTITIES }>();
  mockEntities.mockReturnValue(pending.promise);
  renderLanding(["claude"], { default_project: "/d", default_project_id: "p-b" });
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  focusForm();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).not.toHaveBeenCalled();
  await act(async () => pending.resolve({ projects: ENTITIES }));
  // The starred project and ITS folder — not the global folder a quick click would have used.
  expect(projectValue()).toBe("p-b");
  expect(launchFolder()).toBe("/b");
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("an explicit “No project” has nothing to confirm — Start is not held for the list (#1437)", async () => {
  mockEntities.mockReturnValue(new Promise(() => {}));
  renderLanding(["claude"], { default_project: "/d" }, { restoreDraft: { ...DRAFT, engineChoice: "claude", projectChoice: "" } });
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("Ctrl+Enter outside the form never starts a session — the sidebar, the body (#1437 review)", async () => {
  const user = userEvent.setup();
  render(
    <>
      <a href="/s/claude/x">a sidebar session</a>
    </>,
  );
  renderLanding(["claude"]);
  await findProjects();
  act(() => screen.getByRole("link", { name: "a sidebar session" }).focus());
  await user.keyboard("{Control>}{Enter}{/Control}");
  act(() => (document.activeElement as HTMLElement | null)?.blur());
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).not.toHaveBeenCalledWith(expect.stringMatching(/^\/s\//), expect.anything());
});

test("Ctrl+Enter while a modal is open does not start a session (#1437 review, #1458)", async () => {
  const user = userEvent.setup();
  render(<div role="dialog" aria-modal="true" aria-label="some modal" />);
  renderLanding(["claude"]);
  await findProjects();
  // Focus can still sit on a control INSIDE the form while the dialog is up — the guard must
  // hold for whatever modal renders beside the form (Hermes 6231, retargeted in #1458).
  focusForm();
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).not.toHaveBeenCalledWith(expect.stringMatching(/^\/s\//), expect.anything());
});

test("a held Ctrl+Enter starts ONE session — auto-repeat is ignored (#1437 review)", async () => {
  renderLanding(["claude"]);
  await findProjects();
  focusForm();
  const el = document.activeElement as HTMLElement;
  const press = (repeat: boolean) =>
    act(() => {
      el.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, repeat, bubbles: true, cancelable: true }),
      );
    });
  press(false);
  press(true);
  press(true);
  expect(navigateMock.mock.calls.filter(([to]) => String(to).startsWith("/s/"))).toHaveLength(1);
});

test("“No project” is clickable while the list loads — the way out of a hung request (#1437 review)", async () => {
  const user = userEvent.setup();
  mockEntities.mockReturnValue(new Promise(() => {}));
  renderLanding(["claude"], { default_project: "/d" });
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  await user.click(radioIn(screen.getByRole("radiogroup", { name: "Project" }), "")!);
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("`/` never takes focus from outside the form, nor from a text field in it (#1437 review)", async () => {
  const user = userEvent.setup();
  const many = Array.from({ length: 7 }, (_, i) => ({
    ...ENTITIES[0],
    id: `p-${i}`,
    name: `Project ${i}`,
    folders: [`/p${i}`],
    default_folder: `/p${i}`,
  }));
  mockEntities.mockResolvedValue({ projects: many });
  render(<a href="/s/claude/x">a sidebar session</a>);
  renderLanding(["claude"]);
  await findProjects();
  const filter = screen.getByRole("searchbox", { name: "Filter projects" });
  act(() => screen.getByRole("link", { name: "a sidebar session" }).focus());
  await user.keyboard("/");
  expect(filter).not.toHaveFocus();
  await user.click(filter);
  await user.keyboard("a/b");
  expect(filter).toHaveValue("a/b"); // typed, not swallowed
});

test("Ctrl+Enter on a link inside the form keeps the link's own gesture (#1437 review)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await findProjects();
  act(() => screen.getByRole("link", { name: /new project/i }).focus());
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(navigateMock).not.toHaveBeenCalledWith(expect.stringMatching(/^\/s\//), expect.anything());
});

test("`/` from the body does nothing while a modal is open — any dialog beside the form (#1437 review)", async () => {
  const user = userEvent.setup();
  const many = Array.from({ length: 7 }, (_, i) => ({
    ...ENTITIES[0],
    id: `p-${i}`,
    name: `Project ${i}`,
    folders: [`/p${i}`],
    default_folder: `/p${i}`,
  }));
  mockEntities.mockResolvedValue({ projects: many });
  render(<div role="dialog" aria-modal="true" aria-label="some modal" />);
  renderLanding(["claude"]);
  await findProjects();
  act(() => (document.activeElement as HTMLElement | null)?.blur());
  await user.keyboard("/");
  expect(screen.getByRole("searchbox", { name: "Filter projects" })).not.toHaveFocus();
});


test("fail closed: with no stored default in the config, an API agent starts guarded (#1473, from #1457)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  // No `agent_defaults` at all — an older server, or a config that has not said anything yet.
  renderLanding(["claude", "codex-api"], { default_project: "/d" });
  // The console agent keeps its own documented fallback (on)…
  expect(await screen.findByRole("checkbox", { name: /skip permission prompts/i })).toBeChecked();
  await chooseAgent(user, "codex-api");
  // …but an API agent skips only on a default the config actually states.
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(false);
  expect(api.structuredStart).not.toHaveBeenCalled();
});

test("a skip-capable API agent waits for the config: no Start until the default is known, then it sends what shows (Hermes on #1473)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const tree = (config: AppConfig | null) => (
    <ConfigCtx.Provider value={config}>
      <MemoryRouter
        initialEntries={[
          { pathname: "/", state: { restoreDraft: { ...DRAFT, engineChoice: "codex-api", projectChoice: "" } } },
        ]}
      >
        <Routes>
          <Route path="/" element={<NewSessionLanding />} />
        </Routes>
      </MemoryRouter>
    </ConfigCtx.Provider>
  );
  // The config has not arrived: the API agent shows guarded, and Start waits for the real default.
  const view = render(tree(null));
  await findProjects();
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).not.toBeChecked();
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
  expect(screen.getByTestId("new-session-summary")).toHaveTextContent("Loading your settings…");
  await user.keyboard("{Control>}{Enter}{/Control}");
  expect(api.structuredCreate).not.toHaveBeenCalled();
  // It arrives, ON: the box shows it, and only now can Start send it.
  view.rerender(
    // The folder is the legacy default the config carries — the draft names none (#1458).
    tree({ csrf: "x", new_session_engines: ["codex-api"], terminal_backend: "ws", default_project: "/d", agent_defaults: { bypass: true } }),
  );
  expect(screen.getByRole("checkbox", { name: /skip permission prompts/i })).toBeChecked();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(api.structuredCreate).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(true);
});

test("a skip create opens the session at once; its start is fired, not awaited, and not swallowed (#1505)", async () => {
  const { peekStart, resetStartHandoff } = await import("../components/structured/startHandoff");
  resetStartHandoff();
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  const key = "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: key } as never);
  let refuse: (e: unknown) => void = () => {};
  vi.mocked(api.structuredStart)
    .mockReset()
    .mockReturnValueOnce(new Promise((_resolve, reject) => (refuse = reject)) as never);
  renderLanding(["codex-api"], { default_project: "/d", agent_defaults: { bypass: true } });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  // The page opens while the start is still in flight…
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(navigateMock.mock.calls[0][0]).toBe("/s/codex-api/5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f");
  expect(api.structuredStart).toHaveBeenCalledWith(key);
  // …and its outcome is handed to that session's pane under the same key, never dropped.
  const handed = peekStart(key);
  expect(handed).toBeDefined();
  refuse(new ApiError(409, "the CLI went away"));
  await expect(handed).resolves.toEqual({ kind: "refused", reason: "the CLI went away" });
  resetStartHandoff();
});

test("a guarded create navigates as soon as the create answers (#1505)", async () => {
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(api.structuredStart).not.toHaveBeenCalled();
});

test("New session after a guarded create failed stays guarded with skip-by-default on (#1505)", async () => {
  // Hermes on #1518 (4): the replacement form used to fall back to the stored skip default.
  setRoster(FIXTURE);
  sessionStorage.clear();
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  const state = {
    apiGuardedEngine: "codex-api",
    restoreDraft: {
      engineChoice: "codex-api",
      bypassChoice: null,
      returnTo: null,
      projectChoice: null,
    },
  };
  renderLanding(["claude", "codex-api"], { default_project: "/d", agent_defaults: { bypass: true } }, state);
  const skip = await screen.findByRole("checkbox", { name: /skip permission prompts/i });
  expect(skip).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(api.structuredCreate).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.structuredCreate).mock.calls[0][4]).toBe(false);
  expect(api.structuredStart).not.toHaveBeenCalled();
});

// Start from a specialist (#1693): an API create carries the version and counts it in its
// identity; a later effort change drops it and the create is a plain one.
const POOL = {
  specialists: [
    {
      id: "implementer",
      revision: 1,
      archived: false,
      grade: null,
      live: 0,
      skills_missing: [],
      skills_outdated: [],
      current: {
        version: 2,
        title: "Implementer",
        summary: "",
        skills: [],
        engine: "codex-api",
        model: "gpt-6.1-sol",
        effort: "low",
        capacity: 2,
        created_at: 0,
      },
    },
  ],
  templates: [],
  max_instances: 6,
  live_total: 0,
};

for (const changeEffort of [false, true]) {
  test(`an API create from a specialist ${changeEffort ? "drops it once the effort changes" : "sends its version and preselects its effort"} (#1693)`, async () => {
    setRoster(FIXTURE);
    sessionStorage.clear();
    vi.mocked(api.structuredModels).mockResolvedValue(LEVELS);
    vi.mocked(api.specialists).mockImplementation(() => Promise.resolve(POOL as never));
    try {
      const user = userEvent.setup();
      vi.mocked(api.structuredCreate)
        .mockReset()
        .mockResolvedValue({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
      renderLanding(["codex-api"], DEFAULTS("gpt-6.1-sol", "high"));
      await chooseProject(user, "");
      const picker = await screen.findByRole("combobox", { name: "Specialist" });
      await user.selectOptions(picker, "implementer");
      await vi.waitFor(() => expect(screen.getByTestId("new-session-effort")).toHaveValue("low"));
      expect(screen.getByTestId("new-session-specialist-note")).toHaveTextContent("Implementer v2");
      if (changeEffort) {
        await user.selectOptions(screen.getByTestId("new-session-effort"), "high");
        expect(screen.queryByTestId("new-session-specialist-note")).toBeNull();
      }
      await user.click(screen.getByRole("button", { name: /start session/i }));
      await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
      const [call] = vi.mocked(api.structuredCreate).mock.calls;
      expect(call).toEqual(
        changeEffort
          ? ["codex-api", "/d", call[2], "gpt-6.1-sol", false, "high"]
          : ["codex-api", "/d", call[2], "gpt-6.1-sol", false, "low", { id: "implementer", version: 2 }],
      );
    } finally {
      vi.mocked(api.specialists).mockImplementation(() => Promise.reject(new Error("no pool")));
    }
  });
}


// The New project detour keeps a chosen specialist (review 7113): the draft carries it, and the
// form honours it on the way back only as it was — else it says it was dropped.
const TERMINAL_POOL = (version = 3) => ({
  specialists: [
    {
      id: "implementer",
      revision: 1,
      archived: false,
      grade: null,
      live: 0,
      skills_missing: [],
      skills_outdated: [],
      current: {
        version,
        title: "Implementer",
        summary: "",
        skills: [],
        engine: "claude",
        model: "claude-opus-5",
        effort: null,
        capacity: 2,
        created_at: 0,
      },
    },
  ],
  templates: [],
  max_instances: 6,
  live_total: 0,
});

function withPool(pool: unknown, run: () => Promise<void>) {
  vi.mocked(api.specialists).mockImplementation(() => Promise.resolve(pool as never));
  return run().finally(() =>
    vi.mocked(api.specialists).mockImplementation(() => Promise.reject(new Error("no pool"))),
  );
}

test("the New project detour carries the chosen specialist in the draft (#1693)", () =>
  withPool(TERMINAL_POOL(), async () => {
    const user = userEvent.setup();
    renderLanding(["claude"]);
    await chooseProject(user, "");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Specialist" }), "implementer");
    expect(screen.getByTestId("new-session-specialist-note")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /new project/i }));
    const [, { state }] = navigateMock.mock.calls.at(-1) as [string, { state: { draft: NewSessionDraft } }];
    expect(state.draft.specialistChoice).toEqual({ id: "implementer", version: 3 });
  }));

for (const [current, kept] of [
  [3, true],
  [4, false],
] as const) {
  test(`back from the wizard, the specialist is ${kept ? "restored and launched" : "dropped, and said so"} (${current === 3 ? "same" : "newer"} version) (#1693)`, () =>
    withPool(TERMINAL_POOL(current), async () => {
      const user = userEvent.setup();
      renderLanding(["claude"], { default_project: "/d" }, {
        restoreDraft: {
          engineChoice: "claude",
          bypassChoice: null,
          returnTo: null,
          projectChoice: "",
          modelChoice: { engine: "claude", model: "claude-opus-5" },
          specialistChoice: { id: "implementer", version: 3 },
        },
      });
      await screen.findByRole("combobox", { name: "Specialist" });
      if (kept) {
        await screen.findByTestId("new-session-specialist-note");
        expect(screen.queryByTestId("new-session-specialist-dropped")).toBeNull();
      } else {
        await screen.findByTestId("new-session-specialist-dropped");
        expect(screen.queryByTestId("new-session-specialist-note")).toBeNull();
      }
      navigateMock.mockClear();
      await user.click(screen.getByRole("button", { name: /start session/i }));
      const [, opts] = navigateMock.mock.calls.at(-1) as [
        string,
        { state: { fresh: { specialist?: unknown } } },
      ];
      expect(opts.state.fresh.specialist).toEqual(kept ? { id: "implementer", version: 3 } : undefined);
    }));
}

const RESTORED_DRAFT = {
  restoreDraft: {
    engineChoice: "claude",
    bypassChoice: null,
    returnTo: null,
    projectChoice: "",
    modelChoice: { engine: "claude", model: "claude-opus-5" },
    specialistChoice: { id: "implementer", version: 3 },
  },
};

test("back from the wizard, Start waits for the pool before it honours the specialist (#1693)", async () => {
  let answer: (v: unknown) => void = () => {};
  vi.mocked(api.specialists).mockImplementation(
    () => new Promise((resolve) => (answer = resolve)) as never,
  );
  try {
    const user = userEvent.setup();
    renderLanding(["claude"], { default_project: "/d" }, RESTORED_DRAFT);
    const start = await screen.findByRole("button", { name: /start session/i });
    navigateMock.mockClear();
    await user.click(start);
    expect(navigateMock).not.toHaveBeenCalled(); // never a plain launch of a specialist choice
    answer(TERMINAL_POOL(3));
    await screen.findByTestId("new-session-specialist-note");
    await user.click(screen.getByRole("button", { name: /start session/i }));
    const [, opts] = navigateMock.mock.calls.at(-1) as [
      string,
      { state: { fresh: { specialist?: unknown } } },
    ];
    expect(opts.state.fresh.specialist).toEqual({ id: "implementer", version: 3 });
  } finally {
    vi.mocked(api.specialists).mockImplementation(() => Promise.reject(new Error("no pool")));
  }
});

test("back from the wizard with the pool unreadable, the choice is dropped and said so (#1693)", async () => {
  renderLanding(["claude"], { default_project: "/d" }, RESTORED_DRAFT);
  await screen.findByTestId("new-session-specialist-dropped");
  expect(screen.getByRole("button", { name: /start session/i })).toBeEnabled();
});

test("a specialist on a chat agent is listed but can't be chosen; the chat starts plain (review 7113)", async () => {
  setRoster(FIXTURE);
  const pool = TERMINAL_POOL(3);
  pool.specialists[0].current.engine = "apichat";
  pool.specialists[0].current.model = "default";
  vi.mocked(api.specialists).mockImplementation(() => Promise.resolve(pool as never));
  try {
    const user = userEvent.setup();
    vi.mocked(api.chatNew).mockReset().mockResolvedValue({ id: "apichat:0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b" });
    renderLanding(["apichat"], { default_project: "/d" });
    await chooseProject(user, "");
    const picker = await screen.findByRole("combobox", { name: "Specialist" });
    const option = screen.getByRole("option", { name: /Implementer/ }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(option).toHaveTextContent("a chat agent can’t run a specialist");
    fireEvent.change(picker, { target: { value: "implementer" } }); // even forced
    expect(screen.queryByTestId("new-session-specialist-note")).toBeNull();
    await user.click(screen.getByRole("button", { name: /start session/i }));
    await vi.waitFor(() => expect(api.chatNew).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.chatNew).mock.calls[0]).toEqual(["apichat", "/d"]);
  } finally {
    vi.mocked(api.specialists).mockImplementation(() => Promise.reject(new Error("no pool")));
  }
});

// --- API first (#1728 Phase 1) --------------------------------------------------------------------
//
// The server's `families` block decides a family's default client; the form preselects it, marks
// it, keeps the device's explicit pick for the family, and tells the create whether its client is
// the automatic default or an explicit choice. Engine ids come from the fixture, never a literal.

const API_CLIENT = FIXTURE.find((e) => e.runtime === "api" && e.api?.source)!;
const CONSOLE = FIXTURE.find((e) => e.id === API_CLIENT.api!.source)!;
const FAMILY = API_CLIENT.family!;

function familyRow(
  engine: string | null,
  reason: string,
  reasons: Record<string, string> = {},
) {
  return {
    family: FAMILY,
    clients: [CONSOLE.id, API_CLIENT.id],
    interactive_default: { engine, reason },
    unattended_admitted: [],
    reasons,
  };
}

function apiFirst(row = familyRow(API_CLIENT.id, "api_ready")) {
  localStorage.removeItem("battlelab.familyChoice.v1");
  setRoster(FIXTURE, [], [row]);
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockResolvedValue({ session_key: `${API_CLIENT.id}:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f` } as never);
}

const tileFor = (id: string) =>
  document.querySelector(`label[data-engine="${id}"]`) as HTMLElement;

test("a family with a ready API client preselects it, marked default; Terminal is one tap away (#1728)", async () => {
  apiFirst();
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await findProjects();
  expect(agentValue()).toBe(API_CLIENT.id);
  expect(within(tileFor(API_CLIENT.id)).getByTestId("new-session-family-mark")).toHaveTextContent("default");
  const user = userEvent.setup();
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  // The create says it is the automatic default: the server refuses it if the default moved.
  expect(vi.mocked(api.structuredCreate).mock.calls[0][7]).toBe("automatic");
  setRoster(FIXTURE);
});

test("an unready API client leaves the terminal as the default here, with the reason (#1728)", async () => {
  apiFirst(familyRow(CONSOLE.id, "api_unready", { [API_CLIENT.id]: "the CLI needs an update" }));
  renderLanding([CONSOLE.id], {
    ...GUARDED,
    default_project: "/d",
    unavailable_clients: [{ id: API_CLIENT.id, label: API_CLIENT.label, reason: "the CLI needs an update" }],
  });
  await findProjects();
  expect(agentValue()).toBe(CONSOLE.id);
  expect(tileFor(CONSOLE.id)).toHaveTextContent("default here");
  expect(tileFor(CONSOLE.id)).toHaveTextContent("used because the API client is unavailable");
  const user = userEvent.setup();
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [, opts] = navigateMock.mock.calls[0];
  expect(opts.state.fresh.choice).toBe("automatic");
  setRoster(FIXTURE);
});

test("an explicit Terminal pick is remembered for the family and overrides the API default (#1728)", async () => {
  apiFirst();
  const user = userEvent.setup();
  const first = renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await chooseAgent(user, CONSOLE.id);
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(navigateMock.mock.calls[0][1].state.fresh.choice).toBe("explicit");
  first.unmount();

  // Next time on this device: the Terminal pick wins, says so, and the API is one tap away.
  navigateMock.mockReset();
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await findProjects();
  expect(agentValue()).toBe(CONSOLE.id);
  expect(tileFor(CONSOLE.id)).toHaveTextContent("remembered");
  const hint = screen.getByTestId("new-session-remembered");
  expect(hint).toHaveTextContent(CONSOLE.label);
  await user.click(within(hint).getByRole("button", { name: "Use API" }));
  expect(agentValue()).toBe(API_CLIENT.id);
  expect(localStorage.getItem("battlelab.familyChoice.v1")).not.toContain(CONSOLE.id);
  setRoster(FIXTURE);
});

test("a remembered client that can no longer start is ignored, with the reason (#1728)", async () => {
  apiFirst(familyRow(CONSOLE.id, "api_unready", { [API_CLIENT.id]: "signed out" }));
  localStorage.setItem(
    "battlelab.familyChoice.v1",
    JSON.stringify({ v: 1, choices: { [FAMILY]: API_CLIENT.id } }),
  );
  renderLanding([CONSOLE.id], {
    ...GUARDED,
    default_project: "/d",
    unavailable_clients: [{ id: API_CLIENT.id, label: API_CLIENT.label, reason: "signed out" }],
  });
  await findProjects();
  expect(agentValue()).toBe(CONSOLE.id);
  const stale = screen.getByTestId("new-session-remembered-stale");
  expect(stale).toHaveTextContent(API_CLIENT.label);
  expect(stale).toHaveTextContent("signed out");
  localStorage.removeItem("battlelab.familyChoice.v1");
  setRoster(FIXTURE);
});

test("a stale automatic default is refused: nothing opens, the new default is named (#1728)", async () => {
  apiFirst();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValue(
      new ApiError(409, `the default changed to ${CONSOLE.label} since this was shown — nothing was started`, {
        default: { family: FAMILY, engine: CONSOLE.id, reason: "api_unready" },
      }),
    );
  const user = userEvent.setup();
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const err = await screen.findByTestId("start-error");
  expect(err).toHaveTextContent(`the default changed to ${CONSOLE.label}`);
  expect(err).toHaveTextContent("press Start again");
  expect(navigateMock).not.toHaveBeenCalled();
  setRoster(FIXTURE);
});

test("an older server without families keeps today's default and sends no choice (#1728)", async () => {
  localStorage.removeItem("battlelab.familyChoice.v1");
  setRoster(FIXTURE); // no `families` block
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await findProjects();
  expect(agentValue()).toBe(CONSOLE.id); // the first eligible in roster order, as before
  expect(screen.queryByTestId("new-session-family-mark")).toBeNull();
  const user = userEvent.setup();
  await chooseProject(user, "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(navigateMock.mock.calls[0][1].state.fresh).not.toHaveProperty("choice");
});

test("a Terminal pick made while the API client was unready still overrides it once ready (#1728)", async () => {
  apiFirst(familyRow(CONSOLE.id, "api_unready", { [API_CLIENT.id]: "signed out" }));
  const user = userEvent.setup();
  const first = renderLanding([CONSOLE.id], {
    ...GUARDED,
    default_project: "/d",
    unavailable_clients: [{ id: API_CLIENT.id, label: API_CLIENT.label, reason: "signed out" }],
  });
  await chooseProject(user, "");
  await chooseAgent(user, CONSOLE.id); // explicit, though it is the default here
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  first.unmount();

  // The API client is ready now: the operator's Terminal pick still wins.
  setRoster(FIXTURE, [], [familyRow(API_CLIENT.id, "api_ready")]);
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await findProjects();
  expect(agentValue()).toBe(CONSOLE.id);
  expect(screen.getByTestId("new-session-remembered")).toHaveTextContent(CONSOLE.label);
  localStorage.removeItem("battlelab.familyChoice.v1");
  setRoster(FIXTURE);
});

test("an explicit pick of the API default forgets a remembered Terminal pick (#1728)", async () => {
  apiFirst();
  localStorage.setItem(
    "battlelab.familyChoice.v1",
    JSON.stringify({ v: 1, choices: { [FAMILY]: CONSOLE.id } }),
  );
  const user = userEvent.setup();
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" });
  await chooseProject(user, "");
  expect(agentValue()).toBe(CONSOLE.id);
  await chooseAgent(user, API_CLIENT.id);
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  expect(localStorage.getItem("battlelab.familyChoice.v1")).not.toContain(CONSOLE.id);
  setRoster(FIXTURE);
});

test("an unready stored API default stands in with ITS family's terminal, not the roster's first agent (#1728)", async () => {
  // The stored default is the SECOND family's API client, unready; its console can start, and the
  // first family's agents come earlier in roster order.
  const pairs = FIXTURE.filter((e) => e.runtime === "api" && e.api?.source);
  const second = pairs[1];
  const secondConsole = FIXTURE.find((e) => e.id === second.api!.source)!;
  localStorage.removeItem("battlelab.familyChoice.v1");
  setRoster(FIXTURE, [], [
    familyRow(API_CLIENT.id, "api_ready"),
    {
      family: second.family!,
      clients: [secondConsole.id, second.id],
      interactive_default: { engine: secondConsole.id, reason: "api_unready" },
      unattended_admitted: [],
      reasons: { [second.id]: "signed out" },
    },
  ]);
  renderLanding([CONSOLE.id, API_CLIENT.id, secondConsole.id], {
    ...GUARDED,
    default_project: "/d",
    agent_defaults: { bypass: false, default_engine: second.id },
    unavailable_clients: [{ id: second.id, label: second.label, reason: "signed out" }],
  });
  await findProjects();
  expect(agentValue()).toBe(secondConsole.id);
  expect(tileFor(secondConsole.id)).toHaveTextContent("default here");
  // The notice names the client that actually stands in.
  expect(document.body).toHaveTextContent(`new sessions use ${secondConsole.label} until it is`);
  setRoster(FIXTURE);
});

test("a terminal launch refused as a stale default (4412) comes back explained, Start retries (#1728)", async () => {
  apiFirst();
  renderLanding([CONSOLE.id, API_CLIENT.id], { ...GUARDED, default_project: "/d" }, {
    staleDefault: `the default changed to ${API_CLIENT.label} since this was shown — nothing was started`,
  });
  await findProjects();
  const err = screen.getByTestId("start-error");
  expect(err).toHaveTextContent(`the default changed to ${API_CLIENT.label}`);
  expect(err).toHaveTextContent("press Start again");
  expect(agentValue()).toBe(API_CLIENT.id);
  setRoster(FIXTURE);
});
