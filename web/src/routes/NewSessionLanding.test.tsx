import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import type { NewSessionDraft } from "../lib/newProject";
import { MAP_PATH, NEW_PROJECT_PATH } from "../lib/routes";
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
      projectEntities: vi.fn(),
      createProject: vi.fn(),
      setSessionProject: vi.fn(),
      chatNew: vi.fn(),
      structuredCreate: vi.fn(),
    },
  };
});
// Stub the folder picker (#448): a real tree needs a browser (e2e covers it). The stub exposes a
// button that resolves the pick with a fixed path, so these tests exercise NewSessionLanding's
// wiring (project→folder default, override, new-project default folder, stamping).
vi.mock("../components/FolderPickerModal", () => ({
  FolderPickerModal: ({ onPick }: { onPick: (p: string) => void }) => (
    <div role="dialog" aria-label="folder picker">
      <button type="button" onClick={() => onPick("/picked")}>
        stub-pick
      </button>
    </div>
  ),
}));

const mockEntities = vi.mocked(api.projectEntities);

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

beforeEach(() => {
  navigateMock.mockReset();
  mockEntities.mockReset();
  mockEntities.mockResolvedValue({ projects: ENTITIES });
  vi.mocked(api.setSessionProject)
    .mockReset()
    .mockResolvedValue({ id: "x", project_id: "" });
  vi.mocked(api.createProject).mockReset();
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
    await screen.findByRole("combobox", { name: "Project" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Agent")).not.toBeInTheDocument();
  unmount();
  renderLanding(["claude", "opencode"]);
  expect(await screen.findByText("Agent")).toBeInTheDocument();
});

test("selecting a project prefills the Folder with its default and launches there (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  const project = (await screen.findByRole("combobox", {
    name: "Project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-a"); // first entity is the default selection
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
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
  const project = await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(project, "p-b");
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
  ).toBe("/b");
});

test("Choose folder overrides the project default for this session + stamps the project (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await screen.findByRole("combobox", { name: "Project" }); // default project = Alpha (/a)
  await user.click(screen.getByRole("button", { name: /choose folder/i }));
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
  ).toBe("/picked");

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path, opts] = navigateMock.mock.calls[0] as [
    string,
    { state: { fresh: { cwd: string } } },
  ];
  expect(opts.state.fresh.cwd).toBe("/picked");
  // /picked isn't Alpha's adopted folder → the explicit project must be stamped.
  const id = path.split("/").pop();
  expect(api.setSessionProject).toHaveBeenCalledWith(`claude:${id}`, "p-a");
});

test("'no project' launches in the config default folder without stamping (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"], { default_project: "/d" });
  const project = await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(project, "");
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
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
  renderLanding(["opencode"]);
  await screen.findByRole("combobox", { name: "Project" });
  await user.click(screen.getByRole("button", { name: /choose folder/i })); // override → /picked
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  const id = path.split("/").pop() as string;
  expect(id).toMatch(/^new-[0-9a-f-]{36}$/);
  expect(api.setSessionProject).toHaveBeenCalledWith(`opencode:${id}`, "p-a");
});

const DRAFT: NewSessionDraft = {
  engineChoice: "opencode",
  bypassChoice: false,
  returnTo: null,
  projectChoice: "p-b",
  cwdOverride: "/b/sub",
};

test("+ New project… saves the draft into this entry, then opens the wizard with it (#1187)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "opencode"]);
  const project = await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(project, "");
  await user.selectOptions(screen.getByRole("combobox", { name: /agent/i }), "opencode");
  await user.click(screen.getByRole("checkbox", { name: /skip permission/i }));
  await user.click(screen.getByRole("link", { name: /new project/i }));
  const draft = {
    engineChoice: "opencode",
    bypassChoice: false,
    returnTo: null,
    // The explicit "no project" survives as "", distinct from untouched null.
    projectChoice: "",
    cwdOverride: null,
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
  await screen.findByRole("combobox", { name: "Project" });
  const link = screen.getByRole("link", { name: /new project/i });
  expect(link).toHaveAttribute("href", NEW_PROJECT_PATH);
  link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
  expect(navigateMock).not.toHaveBeenCalled();
});

test("the draft records the map return, so the detour keeps it (#1187)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"], {}, { returnTo: MAP_PATH });
  await screen.findByRole("combobox", { name: "Project" });
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
      restoreDraft: { ...DRAFT, projectChoice: null, cwdOverride: null },
      selectProjectId: "p-b",
    },
  );
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("p-b");
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/b");
  expect((screen.getByRole("combobox", { name: /agent/i }) as HTMLSelectElement).value).toBe(
    "opencode",
  );
  expect(screen.getByRole("checkbox", { name: /skip permission/i })).not.toBeChecked();
  // One-shot: the restore is dropped from the history entry, the map return is kept.
  expect(navigateMock).toHaveBeenCalledWith("/", {
    replace: true,
    state: { returnTo: MAP_PATH },
  });
});

test("cancel: the prior project + folder override come back exactly (#1187)", async () => {
  renderLanding(["claude"], {}, { restoreDraft: DRAFT });
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("p-b");
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/b/sub");
  expect(navigateMock).toHaveBeenCalledWith("/", { replace: true, state: null });
});

test("cancel keeps an explicit “no project” — it never falls back to the default (#1187)", async () => {
  renderLanding(["claude"], {}, { restoreDraft: { ...DRAFT, projectChoice: "", cwdOverride: null } });
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("");
});

test("cancel keeps an UNTOUCHED project choice untouched — the default selection applies (#1187)", async () => {
  renderLanding(
    ["claude"],
    { default_project_id: "p-b" },
    { restoreDraft: { ...DRAFT, projectChoice: null, cwdOverride: null } },
  );
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("p-b");
});

test("malformed restore state is ignored, not trusted (#1187)", async () => {
  renderLanding(["claude"], {}, { restoreDraft: "nope", selectProjectId: 42 });
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("p-a");
  expect(navigateMock).not.toHaveBeenCalled();
});

test("no Project select when there are no entities, but New project is offered (#448)", async () => {
  mockEntities.mockResolvedValue({ projects: [] });
  renderLanding(["claude"]);
  expect(
    await screen.findByRole("link", { name: /new project/i }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Project" })).toBeNull();
});

// ---- Starred default project (#615 Phase 2) -------------------------------------------
//
// Before this pref, the pre-selected project was `entities[0]` — alphabetically first (the
// server sorts by name) and unsettable. `config.default_project_id` names it explicitly.
// `api.projectEntities()` is filtered to unarchived here, so an archived starred project is
// simply absent from `entities` — the same code path as a deleted one.

test("the starred project is pre-selected, not the alphabetically-first one (#615)", async () => {
  renderLanding(["claude"], { default_project_id: "p-b" });
  const project = (await screen.findByRole("combobox", {
    name: "Project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-b");
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
  ).toBe("/b");
});

test("a starred project that no longer exists falls back to the first project (#615)", async () => {
  // Deleted, or archived — `projectEntities()` drops archived, so both look like this.
  renderLanding(["claude"], { default_project_id: "p-gone" });
  const project = (await screen.findByRole("combobox", {
    name: "Project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-a");
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
  ).toBe("/a");
});

test("an archived starred project falls back — it is absent from the entity list (#615)", async () => {
  mockEntities.mockResolvedValue({ projects: [ENTITIES[1]] }); // only Beta survives the archive
  renderLanding(["claude"], { default_project_id: "p-a" });
  const project = (await screen.findByRole("combobox", {
    name: "Project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-b");
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
  const project = (await screen.findByRole("combobox", {
    name: "Project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-a"); // still starred…
  expect(
    (screen.getByLabelText("Launch folder") as HTMLInputElement).value,
  ).toBe("/legacy");
});

// ---- the stored agent defaults (#1128) --------------------------------------------------------------

const FIXTURE = fixture.engines as EngineInfo[];

async function startAndReadFresh(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /start session/i }));
  return navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
}

test("the form starts from the stored default agent and bypass (#1128)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude", "codex"], {
    agent_defaults: { default_engine: "codex", bypass: false },
  });
  await screen.findByRole("combobox", { name: "Project" });
  expect(
    (screen.getByRole("combobox", { name: "Agent" }) as HTMLSelectElement)
      .value,
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
  await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Agent" }),
    "claude",
  );
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
  await screen.findByRole("combobox", { name: "Project" });
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
  await screen.findByRole("combobox", { name: "Project" });
  expect(screen.getByRole("status")).toHaveTextContent(
    "Your default, Gemini CLI, is not installed — new sessions use Claude Code until it is. Your choice is kept.",
  );
  expect(
    (screen.getByRole("combobox", { name: "Agent" }) as HTMLSelectElement)
      .value,
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
  const project = await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(project, "");
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
  await user.selectOptions(await screen.findByRole("combobox", { name: "Project" }), "");
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
  await screen.findByRole("combobox", { name: "Project" });
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
  await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "claude-sonnet-5");
  const [, opts] = await startAndReadFresh(user);
  expect(opts.state.fresh).toEqual({ cwd: "/a", bypass: true, model: "claude-sonnet-5" });
});

test("switching engine clears the model (#1189)", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["claude", "codex"]);
  await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "claude-opus-5");
  await user.selectOptions(screen.getByRole("combobox", { name: "Agent" }), "codex");
  expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe(
    "default",
  );
  // …and back: the claude choice does not come back either.
  await user.selectOptions(screen.getByRole("combobox", { name: "Agent" }), "claude");
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
  await screen.findByRole("combobox", { name: "Project" });
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
    cwdOverride: null,
    modelChoice: { engine: "claude", model: "claude-retired-1" },
  };
  renderLanding(["claude"], {}, { restoreDraft: draft });
  await screen.findByRole("combobox", { name: "Project" });
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
  const select = await screen.findByRole("combobox", { name: "Agent" });
  const groups = Array.from(select.querySelectorAll("optgroup")).map((g) => g.label);
  expect(groups).toEqual(["Console — terminal", "API — structured, no terminal"]);
  const api = select.querySelector('optgroup[label^="API"]')!;
  const options = Array.from(api.querySelectorAll("option"));
  expect(options.map((o) => [o.textContent, o.disabled])).toEqual([
    ["Codex — API", false],
    ["Claude — API — unavailable", true],
  ]);
  expect(screen.getByTestId("new-session-unavailable")).toHaveTextContent(
    "claude 2.1.287 or later is required",
  );
});

test("an API client shows what it is and offers no permission bypass", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  renderLanding(["claude", "codex-api"], { default_project: "/d" });
  expect(await screen.findByRole("checkbox", { name: /skip permission prompts/i })).toBeInTheDocument();
  await user.selectOptions(screen.getByRole("combobox", { name: "Agent" }), "codex-api");
  expect(screen.getByTestId("new-session-api-about")).toHaveTextContent("Codex");
  expect(screen.queryByRole("checkbox", { name: /skip permission prompts/i })).toBeNull();
});

test("starting an API client creates it on the server — single-flight, no bypass, retry reuses the id", async () => {
  setRoster(FIXTURE);
  const user = userEvent.setup();
  vi.mocked(api.structuredCreate)
    .mockReset()
    .mockRejectedValueOnce(new TypeError("network"))
    .mockResolvedValueOnce({ session_key: "codex-api:5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f" } as never);
  renderLanding(["codex-api"], { default_project: "/d" });
  const project = await screen.findByRole("combobox", { name: "Project" });
  await user.selectOptions(project, "");
  const startBtn = screen.getByRole("button", { name: /start session/i });
  await user.click(startBtn);
  await screen.findByTestId("start-error");
  await user.click(startBtn);
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const calls = vi.mocked(api.structuredCreate).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(["codex-api", "/d", calls[0][2]]);
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
  const first = renderLanding(["codex-api"], { default_project: "/d" });
  await user.selectOptions(await screen.findByRole("combobox", { name: "Project" }), "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await screen.findByTestId("start-error");
  first.unmount(); // the page reloads
  renderLanding(["codex-api"], { default_project: "/d" });
  await user.selectOptions(await screen.findByRole("combobox", { name: "Project" }), "");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  await vi.waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
  const [a, b] = vi.mocked(api.structuredCreate).mock.calls;
  expect(b[2]).toBe(a[2]);
  expect(sessionStorage.getItem("battlelab.pendingStructuredCreate")).toBeNull();
});
