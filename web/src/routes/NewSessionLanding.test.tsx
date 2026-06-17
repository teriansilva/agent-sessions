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
      projectEntities: vi.fn(),
      createProject: vi.fn(),
      setSessionProject: vi.fn(),
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
  { id: "p-a", name: "Alpha", color: "", folders: ["/a"], default_folder: "/a", archived: false, created_at: 0, session_count: 1 },
  { id: "p-b", name: "Beta", color: "", folders: ["/b"], default_folder: "/b", archived: false, created_at: 0, session_count: 2 },
];

function renderLanding(engines = ["claude"], extra: Partial<AppConfig> = {}) {
  const config: AppConfig = { csrf: "x", new_session_engines: engines, terminal_backend: "ws", ...extra };
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
  mockEntities.mockReset();
  mockEntities.mockResolvedValue({ projects: ENTITIES });
  vi.mocked(api.setSessionProject).mockReset().mockResolvedValue({ id: "x", project_id: "" });
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
])("mintNewSessionId(%s) → %s", (engine, shape) => {
  expect(mintNewSessionId(engine)).toMatch(shape);
});

test("the agent picker is hidden with one engine, shown with more (#448 reorder)", async () => {
  const { unmount } = renderLanding(["claude"]);
  expect(await screen.findByRole("combobox", { name: "Project" })).toBeInTheDocument();
  expect(screen.queryByText("Agent")).not.toBeInTheDocument();
  unmount();
  renderLanding(["claude", "opencode"]);
  expect(await screen.findByText("Agent")).toBeInTheDocument();
});

test("selecting a project prefills the Folder with its default and launches there (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  const project = (await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement;
  expect(project.value).toBe("p-a"); // first entity is the default selection
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/a");

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: unknown } }];
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
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/b");
});

test("Choose folder overrides the project default for this session + stamps the project (#448)", async () => {
  const user = userEvent.setup();
  renderLanding(["claude"]);
  await screen.findByRole("combobox", { name: "Project" }); // default project = Alpha (/a)
  await user.click(screen.getByRole("button", { name: /choose folder/i }));
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/picked");

  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [path, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: { cwd: string } } }];
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
  expect((screen.getByLabelText("Launch folder") as HTMLInputElement).value).toBe("/d");
  await user.click(screen.getByRole("button", { name: /start session/i }));
  const [, opts] = navigateMock.mock.calls[0] as [string, { state: { fresh: { cwd: string } } }];
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

test("inline create requires a default folder, then creates with it (#448)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject).mockResolvedValue({
    id: "p-new",
    name: "Zed",
    color: "",
    folders: ["/picked"],
    default_folder: "/picked",
    archived: false,
    created_at: 0,
  });
  renderLanding(["claude"]);
  await screen.findByRole("combobox", { name: "Project" });
  await user.click(screen.getByRole("button", { name: /new project/i }));
  await user.type(screen.getByLabelText("New project name"), "Zed");
  // Create is disabled until a default folder is chosen.
  expect(screen.getByRole("button", { name: /^create$/i })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: /default folder/i }));
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  await user.click(screen.getByRole("button", { name: /^create$/i }));
  expect(api.createProject).toHaveBeenCalledWith({ name: "Zed", default_folder: "/picked" });
  expect((await screen.findByRole("combobox", { name: "Project" })) as HTMLSelectElement).toBeInTheDocument();
});

test("no Project select when there are no entities, but New project is offered (#448)", async () => {
  mockEntities.mockResolvedValue({ projects: [] });
  renderLanding(["claude"]);
  expect(await screen.findByRole("button", { name: /new project/i })).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Project" })).toBeNull();
});
