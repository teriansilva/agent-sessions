/** The New project wizard (#1187): the create sequence, its failure states, the return map and the
 *  leave guard. Layout, touch targets and the full browser journeys are Playwright's
 *  (`e2e/new-project-wizard.spec.ts`); this pins the logic a DOM emulator can see. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { NewSessionDraft, WizardEntryState } from "../lib/newProject";
import { COLOR_PRESETS } from "../lib/projectColors";
import {
  DASHBOARD_PATH,
  MISSION_PATH,
  NEW_PROJECT_PATH,
  SESSIONS_PATH,
} from "../lib/routes";
import type { ProjectEntity } from "../types/api";
import NewProject from "./NewProject";

vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      projectEntities: vi.fn(),
      fsDirs: vi.fn(),
      fsMkdir: vi.fn(),
      createProject: vi.fn(),
      setPrefs: vi.fn(),
    },
  };
});
// The picker needs a browser (e2e covers it); the stub resolves a fixed path.
vi.mock("../components/FolderPickerModal", () => ({
  FolderPickerModal: ({ onPick }: { onPick: (p: string) => void }) => (
    <div role="dialog" aria-label="folder picker">
      <button type="button" onClick={() => onPick("/home/u/picked")}>
        stub-pick
      </button>
    </div>
  ),
}));

const SAMPLEPROJECT: ProjectEntity = {
  id: "p-1",
  name: "SampleProject",
  color: COLOR_PRESETS[0],
  folders: ["/home/u/sampleproject"],
  default_folder: "/home/u/sampleproject",
  archived: false,
  created_at: 0,
  session_count: 2,
};

const created = (over: Partial<ProjectEntity> = {}) => ({
  id: "p-new",
  name: "Payments API",
  color: COLOR_PRESETS[1],
  folders: ["/home/u/payments-api"],
  default_folder: "/home/u/payments-api",
  archived: false,
  created_at: 0,
  ...over,
});

const DRAFT: NewSessionDraft = {
  engineChoice: "codex",
  bypassChoice: false,
  returnTo: null,
  projectChoice: "",
  cwdOverride: "/home/u/else",
};

function Probe({ name }: { name: string }) {
  const location = useLocation();
  return (
    <div>
      <h1>{name}</h1>
      <pre data-testid="landed-state">{JSON.stringify(location.state)}</pre>
    </div>
  );
}

const refresh = vi.fn();

function renderWizard(state: WizardEntryState | Record<string, unknown> | null = null) {
  const router = createMemoryRouter(
    [
      { path: NEW_PROJECT_PATH, element: <NewProject /> },
      { path: SESSIONS_PATH, element: <Probe name="New session" /> },
      { path: DASHBOARD_PATH, element: <Probe name="Dashboard" /> },
      { path: MISSION_PATH, element: <Probe name="Missions" /> },
      { path: "/settings/:tab", element: <Probe name="Settings" /> },
    ],
    { initialEntries: [{ pathname: NEW_PROJECT_PATH, state }] },
  );
  render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <RouterProvider router={router} />
    </ConfigRefreshCtx.Provider>,
  );
  return router;
}

const next = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "Next" }));

/** NAME → FOLDER (new, suggested name) → COLOUR → REVIEW. */
async function toReview(user: ReturnType<typeof userEvent.setup>, name = "Payments API") {
  await user.type(screen.getByLabelText("Project name"), name);
  await next(user);
  await screen.findByTestId("np-folder-preview");
  await next(user);
  await next(user);
  await screen.findByRole("heading", { name: "Review and create" });
}

beforeEach(() => {
  refresh.mockReset();
  vi.mocked(api.projectEntities).mockReset().mockResolvedValue({ projects: [SAMPLEPROJECT] });
  vi.mocked(api.fsDirs)
    .mockReset()
    .mockResolvedValue({
      path: "/home/u",
      home: "/home/u",
      dirs: [{ name: "sampleproject", path: "/home/u/sampleproject" }],
    });
  vi.mocked(api.fsMkdir)
    .mockReset()
    .mockImplementation(async (parent, name) => ({ path: `${parent}/${name}` }));
  vi.mocked(api.createProject).mockReset().mockResolvedValue(created());
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
});

test("the full journey: mkdir at CREATE, then the project, with the preselected colour", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "dashboard" });
  await user.type(screen.getByLabelText("Project name"), "Payments API");
  await next(user);
  // The folder name is suggested from the project name, under home; nothing is written yet.
  expect(screen.getByLabelText("Folder name")).toHaveValue("payments-api");
  expect(await screen.findByTestId("np-folder-preview")).toHaveTextContent(
    "~/payments-api created at the end",
  );
  expect(api.fsMkdir).not.toHaveBeenCalled();
  await next(user);
  // SampleProject already uses the first preset, so the second is preselected.
  expect(screen.getByRole("button", { name: `Color ${COLOR_PRESETS[1]}` })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await next(user);
  expect(screen.getByText("created if absent")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Create project" }));
  expect(await screen.findByRole("heading", { name: "Project created" })).toBeInTheDocument();
  expect(api.fsMkdir).toHaveBeenCalledWith("/home/u", "payments-api");
  expect(api.createProject).toHaveBeenCalledWith({
    name: "Payments API",
    color: COLOR_PRESETS[1],
    default_folder: "/home/u/payments-api",
  });
  expect(vi.mocked(api.fsMkdir).mock.invocationCallOrder[0]).toBeLessThan(
    vi.mocked(api.createProject).mock.invocationCallOrder[0],
  );
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("a new folder whose name already exists is labelled existing, reused", async () => {
  const user = userEvent.setup();
  renderWizard();
  await user.type(screen.getByLabelText("Project name"), "SampleProject two");
  await next(user);
  const name = screen.getByLabelText("Folder name");
  await user.clear(name);
  await user.type(name, "sampleproject");
  expect(await screen.findByTestId("np-folder-preview")).toHaveTextContent(
    "existing folder, reused",
  );
  // …and the adopted-folder conflict is flagged at this step, before the server's 409.
  expect(screen.getByTestId("np-folder-owner")).toHaveTextContent("SampleProject");
});

test("an existing folder skips mkdir and creates the project on the pick", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject).mockResolvedValue(
    created({ folders: ["/home/u/picked"], default_folder: "/home/u/picked" }),
  );
  renderWizard();
  await user.type(screen.getByLabelText("Project name"), "Picked");
  await next(user);
  await user.click(screen.getByRole("radio", { name: /existing folder/i }));
  expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Choose folder…" }));
  await user.click(screen.getByRole("button", { name: "stub-pick" }));
  await next(user);
  await next(user);
  expect(screen.getByText("existing")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await screen.findByRole("heading", { name: "Project created" });
  expect(api.fsMkdir).not.toHaveBeenCalled();
  expect(api.createProject).toHaveBeenCalledWith(
    expect.objectContaining({ default_folder: "/home/u/picked" }),
  );
});

test("a 409 keeps REVIEW with the server's reason, names the folder left in place, and links to FOLDER", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject).mockRejectedValue(
    new ApiError(409, "folder '/home/u/payments-api' conflicts with '/home/u' already adopted by project p-9"),
  );
  renderWizard();
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  const alert = await screen.findByTestId("np-create-error");
  expect(alert).toHaveTextContent("conflicts with '/home/u'");
  // mkdir succeeded, so the folder exists — named, never called empty or new.
  expect(alert).toHaveTextContent("The folder ~/payments-api exists and stays where it is.");
  expect(alert.textContent).not.toMatch(/empty|new folder|newly/i);
  expect(screen.getByRole("heading", { name: "Review and create" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Change the folder" }));
  expect(screen.getByRole("heading", { name: "Where does it live?" })).toBeInTheDocument();
});

test("a mkdir refusal stops before the project create", async () => {
  const user = userEvent.setup();
  vi.mocked(api.fsMkdir).mockRejectedValue(new ApiError(422, "invalid folder name"));
  renderWizard();
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  expect(await screen.findByTestId("np-create-error")).toHaveTextContent("invalid folder name");
  expect(api.createProject).not.toHaveBeenCalled();
});

test("a failure that is about no field (a 500) gets no Change-the-… link", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject).mockRejectedValue(new ApiError(500, "POST /api/projects → 500"));
  renderWizard();
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await screen.findByTestId("np-create-error");
  expect(screen.queryByRole("button", { name: /^Change the/ })).toBeNull();
});

test("retrying after a failed create is safe: mkdir again (idempotent), then one project", async () => {
  const user = userEvent.setup();
  vi.mocked(api.createProject)
    .mockRejectedValueOnce(new ApiError(500, ""))
    .mockResolvedValueOnce(created());
  renderWizard();
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await screen.findByTestId("np-create-error");
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await screen.findByRole("heading", { name: "Project created" });
  expect(api.fsMkdir).toHaveBeenCalledTimes(2);
  expect(api.createProject).toHaveBeenCalledTimes(2);
});

test("make-default sets default_project_id; a failure shows on DONE and retries without re-creating", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs)
    .mockRejectedValueOnce(new Error("down"))
    .mockResolvedValueOnce({});
  renderWizard();
  await toReview(user);
  await user.click(screen.getByRole("checkbox", { name: /default project/i }));
  await user.click(screen.getByRole("button", { name: "Create project" }));
  expect(
    await screen.findByText("Created, but not set as your default project."),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByText("It is now your default project.")).toBeInTheDocument();
  expect(api.setPrefs).toHaveBeenNthCalledWith(2, { default_project_id: "p-new" });
  expect(api.createProject).toHaveBeenCalledTimes(1);
  expect(refresh).toHaveBeenCalled();
});

test("Back keeps what was entered, and the rail's Edit links jump to a step", async () => {
  const user = userEvent.setup();
  renderWizard();
  await toReview(user, "Keep me");
  await user.click(screen.getByRole("button", { name: "Back" }));
  await user.click(screen.getByRole("button", { name: "Back" }));
  await user.click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByLabelText("Project name")).toHaveValue("Keep me");
  await next(user);
  expect(screen.getByLabelText("Folder name")).toHaveValue("keep-me");
  await next(user);
  await next(user);
  await user.click(screen.getByRole("button", { name: "Edit name" }));
  expect(screen.getByLabelText("Project name")).toHaveValue("Keep me");
});

test("each step change moves focus to the step heading", async () => {
  const user = userEvent.setup();
  renderWizard();
  await user.type(screen.getByLabelText("Project name"), "Focus");
  await next(user);
  expect(screen.getByRole("heading", { name: "Where does it live?" })).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Back" }));
  expect(screen.getByRole("heading", { name: "Name the project" })).toHaveFocus();
});

test("cancel from New session restores its draft exactly, with no project id", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "new-session", draft: DRAFT });
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  await screen.findByRole("heading", { name: "New session" });
  expect(JSON.parse(screen.getByTestId("landed-state").textContent ?? "null")).toEqual({
    restoreDraft: DRAFT,
  });
});

test("finish from New session goes back with the project selected and the draft's choices", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "new-session", draft: DRAFT });
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await user.click(await screen.findByRole("button", { name: "Done" }));
  await screen.findByRole("heading", { name: "New session" });
  expect(JSON.parse(screen.getByTestId("landed-state").textContent ?? "null")).toEqual({
    restoreDraft: { ...DRAFT, projectChoice: null, cwdOverride: null },
    selectProjectId: "p-new",
  });
});

test("Done returns to the entry point; Plan a mission carries the project", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "settings-projects" });
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await user.click(await screen.findByRole("button", { name: "Done" }));
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
});

test("Plan a mission here preselects the new project", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "dashboard" });
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await user.click(await screen.findByRole("button", { name: "Plan a mission here" }));
  await screen.findByRole("heading", { name: "Missions" });
  expect(JSON.parse(screen.getByTestId("landed-state").textContent ?? "null")).toEqual({
    missionProjectId: "p-new",
  });
});

test("an unknown entry point gets no Done — only DONE's own actions", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "https://evil.example" });
  await toReview(user);
  await user.click(screen.getByRole("button", { name: "Create project" }));
  await screen.findByRole("heading", { name: "Project created" });
  expect(screen.queryByRole("button", { name: "Done" })).toBeNull();
  expect(screen.getByRole("button", { name: "Start a session" })).toBeInTheDocument();
});

test("leaving a dirty draft asks first; a clean one leaves at once", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "dashboard" });
  await user.type(screen.getByLabelText("Project name"), "Half done");
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("has not been saved");
  await user.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("Project name")).toHaveValue("Half done");
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  await user.click(await screen.findByRole("button", { name: "Discard and leave" }));
  expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
});

test("a clean wizard cancels without a question", async () => {
  const user = userEvent.setup();
  renderWizard({ from: "dashboard" });
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
});

test("a name clash with an active project warns but does not block", async () => {
  const user = userEvent.setup();
  renderWizard();
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  await user.type(screen.getByLabelText("Project name"), "sampleproject");
  expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
});
