import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import type { AppConfig } from "../types/api";
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
      folders: vi.fn(),
      setPrefs: vi.fn().mockResolvedValue({}),
      mkdir: vi.fn(),
      projectEntities: vi.fn(),
      createProject: vi.fn(),
      setSessionProject: vi.fn(),
    },
  };
});
const mockProjects = vi.mocked(api.folders);
const mockEntities = vi.mocked(api.projectEntities);

function renderLanding(engines = ["claude"], extra: Partial<AppConfig> = {}) {
  const config: AppConfig = {
    csrf: "x",
    new_session_engines: engines,
    terminal_backend: "ws",
    ...extra,
  };
  return render(
    <ConfigCtx.Provider value={config}>
      <MemoryRouter>
        <NewSessionLanding />
      </MemoryRouter>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  navigateMock.mockReset();
  mockProjects.mockReset();
  mockEntities.mockReset();
  mockEntities.mockResolvedValue({ projects: [] });
  vi.mocked(api.setSessionProject).mockReset();
  vi.mocked(api.setSessionProject).mockResolvedValue({ id: "x", project_id: "" });
  vi.mocked(api.createProject).mockReset();
});

test("starts a session: mints an id and navigates with the fresh launch params", async () => {
  const user = userEvent.setup();
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/home/m/proj", label: "/home/m/proj" }] });
  renderLanding();
  await screen.findByRole("option", { name: "/home/m/proj" });
  // The picker mirrors the curated sidebar (#335): it must request the filtered list.
  expect(mockProjects).toHaveBeenCalledWith({ visible: true });

  await user.click(screen.getByRole("button", { name: /start session/i }));
  expect(navigateMock).toHaveBeenCalledTimes(1);
  const [path, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
  expect(path).toMatch(/^\/s\/claude\/[0-9a-f-]{36}$/); // engine + a minted uuid
  expect(opts.state.fresh).toEqual({ cwd: "/home/m/proj", bypass: true });
});

test.each([
  ["claude", /^[0-9a-f-]{36}$/],
  ["codex", /^[0-9a-f-]{36}$/],
  ["gemini", /^[0-9a-f-]{36}$/],
  ["opencode", /^new-[0-9a-f-]{36}$/], // #163: opencode needs the new-<uuid> placeholder
])("mintNewSessionId(%s) → %s", (engine, shape) => {
  expect(mintNewSessionId(engine)).toMatch(shape);
});

test("opencode new session navigates to a new-<uuid> placeholder, not a bare uuid (#163)", async () => {
  const user = userEvent.setup();
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/home/m/proj", label: "/home/m/proj" }] });
  renderLanding(["opencode"]); // single engine → opencode is the effective selection
  await screen.findByRole("option", { name: "/home/m/proj" });

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  // A bare uuid here would 4404 on the opencode new=1 launch.
  expect(path).toMatch(/^\/s\/opencode\/new-[0-9a-f-]{36}$/);
});

test("the agent picker is hidden when there is only one engine", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/x", label: "/x" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/x" });
  expect(screen.queryByText("Agent")).not.toBeInTheDocument();
});

test("the agent picker is shown with more than one engine", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/x", label: "/x" }] });
  renderLanding(["claude", "opencode"]);
  expect(await screen.findByText("Agent")).toBeInTheDocument();
});

test("Start is disabled until a project is available", async () => {
  mockProjects.mockResolvedValue({ folders: [] });
  renderLanding();
  // No projects → the only option is the placeholder and Start stays disabled.
  await screen.findByRole("option", { name: /no projects found/i });
  expect(screen.getByRole("button", { name: /start session/i })).toBeDisabled();
});

test("pre-selects the default project when it is pickable (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({
    folders: [
      { cwd: "/a", label: "/a" },
      { cwd: "/b", label: "/b" },
    ],
  });
  renderLanding(["claude"], { default_project: "/b" });
  await screen.findByRole("option", { name: "/b" });
  // the select lands on the default, not the first option
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/b");
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: { cwd: string } } }];
  expect(opts.state.fresh.cwd).toBe("/b");
});

test("falls back to the first project when the default is stale (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/a", label: "/a" }] });
  renderLanding(["claude"], { default_project: "/gone" });
  await screen.findByRole("option", { name: "/a" });
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/a");
});

test("Set as default persists the selected project (#335 Phase 2)", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/a", label: "/a" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/a" });
  // accessible name comes from the aria-label (verbose for screen readers)
  await userEvent.click(screen.getByRole("button", { name: /set the selected project as the default/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project: "/a" });
  // after saving it reflects the default state (aria-label flips)
  expect(screen.getByRole("button", { name: /this is your default project/i })).toBeDisabled();
});

test("create-folder makes a dir under a root and selects it (#335 Phase 3)", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/code/a", label: "/code/a" }] });
  vi.mocked(api.mkdir).mockResolvedValue({ cwd: "/code/newproj" });
  renderLanding(["claude"], { project_roots: ["/code"] });
  await screen.findByRole("option", { name: "/code/a" });
  await userEvent.click(screen.getByRole("button", { name: /new folder/i }));
  await userEvent.type(screen.getByLabelText(/new folder name/i), "newproj");
  await userEvent.click(screen.getByRole("button", { name: /^create$/i }));
  expect(api.mkdir).toHaveBeenCalledWith("/code", "newproj");
  // the new (not-yet-pickable) dir becomes selectable + is selected
  await screen.findByRole("option", { name: "/code/newproj" });
  expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("/code/newproj");
});

test("no New folder control when no roots are configured (#335 Phase 3)", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/code/a", label: "/code/a" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/code/a" });
  expect(screen.queryByRole("button", { name: /new folder/i })).toBeNull();
});

// ---- Project entity picker (#361 Phase 3) ----

const ENTITIES = [
  { id: "p-a", name: "Alpha", color: "", folders: ["/a"], archived: false, created_at: 0, session_count: 1 },
  { id: "p-b", name: "Beta", color: "", folders: ["/b"], archived: false, created_at: 0, session_count: 2 },
];

function mockTwoFoldersWithEntities() {
  mockProjects.mockResolvedValue({
    folders: [
      { cwd: "/a", label: "/a" },
      { cwd: "/b", label: "/b" },
    ],
  });
  mockEntities.mockResolvedValue({ projects: ENTITIES });
}

test("the project select defaults to the owning entity of the selected folder (#361)", async () => {
  mockTwoFoldersWithEntities();
  renderLanding(["claude"]);
  const sel = (await screen.findByRole("combobox", {
    name: "Assign to project",
  })) as HTMLSelectElement;
  // /a is the first (selected) folder → Alpha owns it.
  expect(sel.value).toBe("p-a");
  expect(screen.getByRole("option", { name: "none (group by folder)" })).toBeInTheDocument();
});

test("while untouched, the project select follows the folder selection (#361)", async () => {
  mockTwoFoldersWithEntities();
  renderLanding(["claude"]);
  const project = (await screen.findByRole("combobox", {
    name: "Assign to project",
  })) as HTMLSelectElement;
  const folder = screen.getByRole("combobox", { name: "Folder" });
  await userEvent.selectOptions(folder, "/b");
  expect(project.value).toBe("p-b");
  // The owning entity IS the folder-resolution result — Start must not stamp it.
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  expect(api.setSessionProject).not.toHaveBeenCalled();
});

test("choosing none never stamps a project (#361)", async () => {
  mockTwoFoldersWithEntities();
  renderLanding(["claude"]);
  const project = await screen.findByRole("combobox", { name: "Assign to project" });
  await userEvent.selectOptions(project, "");
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  expect(navigateMock).toHaveBeenCalledTimes(1);
  expect(api.setSessionProject).not.toHaveBeenCalled();
});

test("an explicit non-default choice stamps the project with the navigated session key (#361)", async () => {
  mockTwoFoldersWithEntities();
  renderLanding(["claude"]);
  const project = await screen.findByRole("combobox", { name: "Assign to project" });
  // cwd stays /a (owned by Alpha) but the user explicitly picks Beta.
  await userEvent.selectOptions(project, "p-b");
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  const id = path.split("/").pop();
  expect(api.setSessionProject).toHaveBeenCalledWith(`claude:${id}`, "p-b");
});

test("opencode stamping uses the new-<uuid> placeholder key the navigation uses (#361)", async () => {
  mockTwoFoldersWithEntities();
  renderLanding(["opencode"]);
  const project = await screen.findByRole("combobox", { name: "Assign to project" });
  await userEvent.selectOptions(project, "p-b");
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  const [path] = navigateMock.mock.calls[0] as [string, unknown];
  const id = path.split("/").pop() as string;
  expect(id).toMatch(/^new-[0-9a-f-]{36}$/);
  expect(api.setSessionProject).toHaveBeenCalledWith(`opencode:${id}`, "p-b");
});

test("inline create makes a standalone project and selects it (#361)", async () => {
  mockTwoFoldersWithEntities();
  vi.mocked(api.createProject).mockResolvedValue({
    id: "p-new",
    name: "Zed",
    color: "",
    folders: [],
    archived: false,
    created_at: 0,
  });
  renderLanding(["claude"]);
  await screen.findByRole("combobox", { name: "Assign to project" });
  await userEvent.click(screen.getByRole("button", { name: /new project/i }));
  await userEvent.type(screen.getByLabelText("New project name"), "Zed");
  await userEvent.click(screen.getByRole("button", { name: /^create$/i }));
  // Standalone (no folder adoption — that lives in Settings).
  expect(api.createProject).toHaveBeenCalledWith({ name: "Zed" });
  const project = (await screen.findByRole("combobox", {
    name: "Assign to project",
  })) as HTMLSelectElement;
  expect(project.value).toBe("p-new");
  // A folder-less project is never the folder-resolution result → Start stamps it.
  await userEvent.click(screen.getByRole("button", { name: /start session/i }));
  expect(api.setSessionProject).toHaveBeenCalledWith(expect.stringMatching(/^claude:/), "p-new");
});

test("no project select renders when there are no entities (#361)", async () => {
  mockProjects.mockResolvedValue({ folders: [{ cwd: "/a", label: "/a" }] });
  renderLanding(["claude"]);
  await screen.findByRole("option", { name: "/a" });
  expect(screen.queryByRole("combobox", { name: "Assign to project" })).toBeNull();
  // …but the create affordance is how the FIRST project gets made.
  expect(screen.getByRole("button", { name: /new project/i })).toBeInTheDocument();
});
