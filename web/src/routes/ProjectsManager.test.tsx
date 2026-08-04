import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type {
  AppConfig,
  ProjectArchiveReport,
  ProjectEntity,
} from "../types/api";
import { ProjectsManagerCard } from "./ProjectsManager";

vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      projectEntities: vi.fn(),
      createProject: vi.fn(),
      patchProject: vi.fn(),
      deleteProject: vi.fn(),
      archiveProject: vi.fn(),
      unarchiveProject: vi.fn(),
      folders: vi.fn(),
      setPrefs: vi.fn(),
    },
  };
});
// Stub the folder picker (#448) — a real tree needs a browser (e2e covers it). Resolves the pick
// with a fixed path so the create/default-folder flows are exercisable here.
vi.mock("../components/FolderPickerModal", () => ({
  FolderPickerModal: ({ onPick }: { onPick: (p: string) => void }) => (
    <div role="dialog" aria-label="folder picker">
      <button type="button" onClick={() => onPick("/picked")}>
        stub-pick
      </button>
    </div>
  ),
}));

function ent(over: Partial<ProjectEntity> = {}): ProjectEntity {
  return {
    id: "p-1",
    name: "Cayoo",
    color: "#5fd7ff",
    folders: ["/home/u/cayoo"],
    default_folder: "/home/u/cayoo",
    archived: false,
    created_at: 0,
    session_count: 3,
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.folders).mockResolvedValue({ folders: [] });
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [ent()] });
  // The star's write is a promise chain — a bare vi.fn() returns undefined and `.then` throws
  // *after* the optimistic flip, so assertions pass while the click actually errored.
  vi.mocked(api.setPrefs).mockResolvedValue({});
});

afterEach(() => {
  vi.restoreAllMocks();
});

test("lists entities with name, member count, and adopted-folder chips", async () => {
  render(<ProjectsManagerCard />);
  expect(await screen.findByText("Cayoo")).toBeInTheDocument();
  expect(screen.getByText("3 sessions")).toBeInTheDocument();
  // ~/cayoo shows both as the default-folder path and the adopted-folder chip (#448).
  expect(screen.getAllByText("~/cayoo").length).toBeGreaterThan(0);
  expect(screen.getByText(/default folder:/i)).toBeInTheDocument();
  // This manager is the unarchive surface — it must request archived entities too.
  expect(api.projectEntities).toHaveBeenCalledWith({ includeArchived: true });
  // The metadata invariant is part of the panel copy.
  expect(screen.getByText(/never moves session files/i)).toBeInTheDocument();
});

test("create requires a default folder, then calls the API and refetches (#448)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject).mockResolvedValue({
    id: "p-2",
    name: "Fresh",
    color: "",
    folders: ["/picked"],
    default_folder: "/picked",
    archived: false,
    created_at: 0,
  });
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      ent(),
      ent({
        id: "p-2",
        name: "Fresh",
        folders: ["/picked"],
        default_folder: "/picked",
        session_count: 0,
      }),
    ],
  });
  await user.type(screen.getByLabelText("New project name"), "Fresh");
  // Create stays disabled until a default folder is chosen (#448).
  expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "Choose the default folder" }),
  );
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  await user.click(screen.getByRole("button", { name: "Create" }));
  expect(api.createProject).toHaveBeenCalledWith({
    name: "Fresh",
    default_folder: "/picked",
  });
  expect(await screen.findByText("Fresh")).toBeInTheDocument();
  expect(api.projectEntities).toHaveBeenCalledTimes(2);
});

test("changing a project's default folder patches default_folder (#448)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue({
    id: "p-1",
    name: "Cayoo",
    color: "#5fd7ff",
    folders: ["/home/u/cayoo", "/picked"],
    default_folder: "/picked",
    archived: false,
    created_at: 0,
  });
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", { name: "Change default folder for Cayoo" }),
  );
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", {
    default_folder: "/picked",
  });
});

test("archive with a failed member shows the failed list and Retry re-calls the endpoint", async () => {
  const user = userEvent.setup();
  const report: ProjectArchiveReport = {
    id: "p-1",
    archived: true,
    sessions: [
      { id: "claude:aaaa", result: "archived" },
      { id: "opencode:bbbb", result: "failed", reason: "db locked" },
    ],
    counts: { archived: 1, already_archived: 0, failed: 1 },
  };
  vi.mocked(api.archiveProject).mockResolvedValue(report);
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", { name: "Archive project Cayoo" }),
  );
  expect(api.archiveProject).toHaveBeenCalledWith("p-1");
  expect(
    await screen.findByText(/1 archived · 0 already archived · 1 failed/),
  ).toBeInTheDocument();
  expect(screen.getByText("opencode:bbbb")).toBeInTheDocument();
  expect(screen.getByText(/db locked/)).toBeInTheDocument();
  // Retry blindly re-calls the SAME endpoint — already_* results are normal.
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(api.archiveProject).toHaveBeenCalledTimes(2);
  expect(api.archiveProject).toHaveBeenLastCalledWith("p-1");
});

test("delete asks for confirmation (files-never-touched copy) before calling the API", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(api.deleteProject).mockResolvedValue({ deleted: true, id: "p-1" });
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", { name: "Delete project Cayoo" }),
  );
  expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/never touched/i));
  expect(api.deleteProject).toHaveBeenCalledWith("p-1");
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalledTimes(2));
});

test("a declined confirm aborts the delete", async () => {
  const user = userEvent.setup();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", { name: "Delete project Cayoo" }),
  );
  expect(api.deleteProject).not.toHaveBeenCalled();
});

test("archived entities sit in a collapsed subsection with an Unarchive action", async () => {
  const user = userEvent.setup();
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      ent(),
      ent({ id: "p-9", name: "Old", archived: true, session_count: 1 }),
    ],
  });
  vi.mocked(api.unarchiveProject).mockResolvedValue({
    id: "p-9",
    archived: false,
    sessions: [{ id: "claude:cccc", result: "unarchived" }],
    counts: { unarchived: 1, already_unarchived: 0, failed: 0 },
  });
  render(<ProjectsManagerCard />);
  expect(await screen.findByText("Archived (1)")).toBeInTheDocument();
  // The archived row is NOT in the active list (no archive/delete actions for it).
  expect(
    screen.queryByRole("button", { name: "Archive project Old" }),
  ).toBeNull();
  await user.click(
    screen.getByRole("button", { name: "Unarchive project Old" }),
  );
  expect(api.unarchiveProject).toHaveBeenCalledWith("p-9");
  expect(await screen.findByText(/1 unarchived/)).toBeInTheDocument();
  expect(api.projectEntities).toHaveBeenCalledTimes(2);
});

test("a 409 folder conflict surfaces the server's detail string inline", async () => {
  const user = userEvent.setup();
  vi.mocked(api.folders).mockResolvedValue({
    folders: [{ cwd: "/home/u/free", label: "/home/u/free" }],
  });
  vi.mocked(api.patchProject).mockRejectedValue(
    new ApiError(
      409,
      "folder '/home/u/free' conflicts with '/home/u/free' already adopted by project p-7",
    ),
  );
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.selectOptions(
    await screen.findByLabelText("Adopt a folder into Cayoo"),
    "/home/u/free",
  );
  await user.click(screen.getByRole("button", { name: "Add" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", {
    folders: ["/home/u/cayoo", "/home/u/free"],
  });
  expect(
    await screen.findByText(/already adopted by project p-7/),
  ).toBeInTheDocument();
});

test("releasing a folder chip patches the remaining folder set", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue({
    id: "p-1",
    name: "Cayoo",
    color: "#5fd7ff",
    folders: [],
    archived: false,
    created_at: 0,
  });
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", {
      name: "Release folder /home/u/cayoo from Cayoo",
    }),
  );
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { folders: [] });
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalledTimes(2));
});

test("rename commits via patchProject and color swatches set/clear the color", async () => {
  const user = userEvent.setup();
  vi.mocked(api.patchProject).mockResolvedValue({
    id: "p-1",
    name: "Cayoo 2",
    color: "",
    folders: ["/home/u/cayoo"],
    archived: false,
    created_at: 0,
  });
  render(<ProjectsManagerCard />);
  await screen.findByText("Cayoo");
  await user.click(
    screen.getByRole("button", { name: "Rename project Cayoo" }),
  );
  const input = screen.getByRole("textbox", { name: "Project name for Cayoo" });
  await user.clear(input);
  await user.type(input, "Cayoo 2");
  await user.click(screen.getByRole("button", { name: "Save name for Cayoo" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { name: "Cayoo 2" });

  await user.click(screen.getByRole("button", { name: "Set color for Cayoo" }));
  await user.click(screen.getByRole("button", { name: "Color #ffb000" }));
  expect(api.patchProject).toHaveBeenCalledWith("p-1", { color: "#ffb000" });
});

// ---- Default project star (#615 Phase 2) ------------------------------------------------
//
// The star replaced the Settings "Default project" card, which stored a bare cwd
// (`default_project`). That pref had been shadowed since #448 — New Session resolves
// `selectedProject.default_folder ?? config.default_project`, so with any project present the
// cwd never fired — while the project actually pre-selected was `entities[0]`: alphabetically
// first, and unsettable. The star names the PROJECT.

/** Mounts the card with a config value so the star can seed from `default_project_id`. */
function renderWithConfig(config: Partial<AppConfig> = {}) {
  const cfg = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    auth_mode: "single-user",
    two_factor_enabled: false,
    ...config,
  } as AppConfig;
  return render(
    <ConfigCtx.Provider value={cfg}>
      <ProjectsManagerCard />
    </ConfigCtx.Provider>,
  );
}

test("star: unstarred by default, and starring persists default_project_id", async () => {
  renderWithConfig();
  const star = await screen.findByRole("button", {
    name: "Make Cayoo the default project",
  });
  expect(star).toHaveAttribute("aria-pressed", "false");
  await userEvent.click(star);
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project_id: "p-1" });
  // Optimistic: the row flips before the config refresh lands.
  expect(
    await screen.findByRole("button", {
      name: "Cayoo is the default project — clear it",
    }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("star: seeds from config.default_project_id, and clicking the starred one clears it", async () => {
  renderWithConfig({ default_project_id: "p-1" });
  const star = await screen.findByRole("button", {
    name: "Cayoo is the default project — clear it",
  });
  expect(star).toHaveAttribute("aria-pressed", "true");
  // Clicking the starred project clears the preference — New Session falls back to the first
  // project, exactly as it behaved before the pref existed.
  await userEvent.click(star);
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project_id: "" });
  expect(
    await screen.findByRole("button", {
      name: "Make Cayoo the default project",
    }),
  ).toHaveAttribute("aria-pressed", "false");
});

test("star: exactly one project is starred at a time", async () => {
  vi.mocked(api.projectEntities).mockResolvedValue({
    projects: [
      ent(),
      ent({ id: "p-2", name: "BattleLab", folders: ["/home/u/bl"] }),
    ],
  });
  renderWithConfig({ default_project_id: "p-1" });
  await screen.findByRole("button", {
    name: "Cayoo is the default project — clear it",
  });
  const other = screen.getByRole("button", {
    name: "Make BattleLab the default project",
  });
  await userEvent.click(other);
  expect(api.setPrefs).toHaveBeenCalledWith({ default_project_id: "p-2" });
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Make Cayoo the default project" }),
    ).toHaveAttribute("aria-pressed", "false"),
  );
});

test("star: a failed save rolls back and surfaces an error", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("nope"));
  renderWithConfig();
  await userEvent.click(
    await screen.findByRole("button", {
      name: "Make Cayoo the default project",
    }),
  );
  expect(
    await screen.findByText(/couldn.t save the default project/i),
  ).toBeInTheDocument();
  // Rolled back to unstarred — never leave the UI asserting a default the server rejected.
  expect(
    screen.getByRole("button", { name: "Make Cayoo the default project" }),
  ).toHaveAttribute("aria-pressed", "false");
});
