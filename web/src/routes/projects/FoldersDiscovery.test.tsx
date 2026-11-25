/** HQ → Projects → Folders & discovery (#2143): Settings → Projects' Folder discovery and Session
 *  overview tests, moved with the cards (#174, #465, #470, #615). The checkbox is a NIGHTJAR
 *  `Switch` now (on = shown); its accessible names are unchanged. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../../app/config";
import { OverviewPrefsProvider } from "../../app/OverviewPrefsContext";
import { api, ApiError } from "../../lib/api";
import type { AppConfig, ProjectEntity } from "../../types/api";
import { FoldersDiscovery } from "./FoldersDiscovery";

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: { folders: vi.fn(), projectEntities: vi.fn(), setPrefs: vi.fn() },
  };
});
vi.mock("../../components/FolderPickerModal", () => ({
  FolderPickerModal: ({ onPick }: { onPick: (p: string) => void }) => (
    <div role="dialog" aria-label="folder picker">
      <button type="button" onClick={() => onPick("/home/u")}>
        stub-pick
      </button>
    </div>
  ),
}));

const base: AppConfig = {
  csrf: "t",
  new_session_engines: [],
  terminal_backend: "ws",
  auth_mode: "single-user",
  two_factor_enabled: false,
} as AppConfig;

const alphaProject: ProjectEntity = {
  id: "p-1",
  name: "Alpha Project",
  color: "#c02020",
  folders: ["/home/u/alpha"],
  default_folder: "/home/u/alpha",
  archived: false,
  created_at: 0,
  session_count: 1,
};

function tree(cfg: AppConfig | null) {
  return (
    <MemoryRouter>
      <ConfigCtx.Provider value={cfg}>
        <OverviewPrefsProvider>
          <FoldersDiscovery />
        </OverviewPrefsProvider>
      </ConfigCtx.Provider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.folders).mockResolvedValue({ folders: [] });
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [] });
  vi.mocked(api.setPrefs).mockResolvedValue({});
});

test("switching a folder off hides it and persists projects_hidden (#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/beta", label: "Beta" },
    ],
  });
  render(tree(base));
  const alpha = await screen.findByRole("switch", { name: /~\/alpha/i });
  expect(alpha).toHaveAttribute("aria-checked", "true");
  await userEvent.click(alpha);
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_hidden: ["/home/u/alpha"] });
  await waitFor(() => expect(screen.getByRole("switch", { name: /~\/alpha/i })).toHaveAttribute("aria-checked", "false"));
});

test("renaming an unassigned folder persists via setProjectName (#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({ folders: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
  render(tree(base));
  await userEvent.click(await screen.findByRole("button", { name: /rename ~\/alpha/i }));
  const input = await screen.findByRole("textbox", { name: /custom name for \/home\/u\/alpha/i });
  await userEvent.type(input, "My Alpha");
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ project_names: { "/home/u/alpha": "My Alpha" } });
});

test("a name seeded after /api/config resolves is shown on the row (#161/#174)", async () => {
  vi.mocked(api.folders).mockResolvedValue({ folders: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
  const { rerender } = render(tree(null));
  await screen.findByRole("button", { name: /rename ~\/alpha/i });
  expect(screen.queryByText("Saved Alpha")).not.toBeInTheDocument();
  rerender(tree({ ...base, project_names: { "/home/u/alpha": "Saved Alpha" } }));
  expect(await screen.findByText("Saved Alpha")).toBeInTheDocument();
});

test("folders group under their owning project, an Unassigned group last; only unassigned ones rename (#465, #615)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/loose", label: "Loose" },
    ],
  });
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [alphaProject] });
  render(tree(base));
  const alphaList = await screen.findByRole("list", { name: /folders in alpha project/i });
  const unassigned = screen.getByRole("list", { name: /folders in unassigned/i });
  expect(within(unassigned).getByRole("button", { name: /rename ~\/loose/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /rename ~\/alpha/i })).not.toBeInTheDocument();
  expect(alphaList).toHaveTextContent("~/alpha");
  expect(screen.getByTitle(/named by its project/i)).toHaveTextContent("~/alpha");
  // The group links to the project's own page, where its folders are managed.
  expect(screen.getByRole("link", { name: "Open Alpha Project" })).toHaveAttribute("href", "/projects/p-1");
});

test("each switch's name states what switching off does, per row kind (#615)", async () => {
  vi.mocked(api.folders).mockResolvedValue({
    folders: [
      { cwd: "/home/u/alpha", label: "Alpha" },
      { cwd: "/home/u/loose", label: "Loose" },
    ],
  });
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [alphaProject] });
  render(tree(base));
  expect(await screen.findByRole("switch", { name: "Offer ~/alpha as a launch location" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Show ~/loose in the sidebar, filter, and overview" })).toBeInTheDocument();
  expect(screen.queryByText(/hide it everywhere/i)).not.toBeInTheDocument();
});

test("the two visibility modes are a Seg; choosing Only included persists the mode (#335)", async () => {
  render(tree(base));
  const group = await screen.findByRole("radiogroup", { name: "Project visibility" });
  expect(within(group).getByRole("radio", { name: /show all/i })).toHaveAttribute("aria-checked", "true");
  await userEvent.click(within(group).getByRole("radio", { name: /only included/i }));
  expect(api.setPrefs).toHaveBeenCalledWith(expect.objectContaining({ projects_mode: "included" }));
});

test("roots and exclusions show with Remove; removing a root persists the list (#465)", async () => {
  render(tree({ ...base, project_roots: ["/home/u/code"], folder_exclusions: ["/home/u/code/scratch"] }));
  expect(await screen.findByRole("heading", { name: /project roots/i })).toBeInTheDocument();
  expect(screen.getByRole("list", { name: /root directories/i })).toBeInTheDocument();
  expect(screen.getByRole("list", { name: /excluded folders/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /remove root ~\/code/i }));
  expect(api.setPrefs).toHaveBeenCalledWith({ project_roots: [] });
});

test("picking a root through the folder picker commits it (#465)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({ project_roots: ["/home/u"] });
  render(tree(base));
  await userEvent.click(await screen.findByRole("button", { name: /add root…/i }));
  await userEvent.click(screen.getByRole("button", { name: "stub-pick" }));
  expect(api.setPrefs).toHaveBeenCalledWith({ project_roots: ["/home/u"] });
});

test("a failed save of the roots rolls back and says so", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new ApiError(500, "disk full"));
  render(tree({ ...base, project_roots: ["/home/u/code"] }));
  await userEvent.click(await screen.findByRole("button", { name: /remove root ~\/code/i }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/disk full/);
  expect(screen.getByRole("button", { name: /remove root ~\/code/i })).toBeInTheDocument();
});

test("the folder list re-reads when the discovery scope changes (#470)", async () => {
  const { rerender } = render(tree({ ...base, project_roots: [] }));
  await screen.findByRole("heading", { name: /overview visibility/i });
  await waitFor(() => expect(api.folders).toHaveBeenCalledTimes(1));
  rerender(tree({ ...base, project_roots: ["/home/u/code"] }));
  await waitFor(() => expect(api.folders).toHaveBeenCalledTimes(2));
  rerender(tree({ ...base, project_roots: ["/home/u/code"], folder_exclusions: ["/home/u/code/scratch"] }));
  await waitFor(() => expect(api.folders).toHaveBeenCalledTimes(3));
});

test("a refresh after a scope change that fails keeps the last rows, marked stale, with Retry", async () => {
  vi.mocked(api.folders).mockResolvedValueOnce({ folders: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
  const { rerender } = render(tree({ ...base, project_roots: [] }));
  expect(await screen.findByRole("switch", { name: /~\/alpha/i })).toBeInTheDocument();
  vi.mocked(api.folders).mockRejectedValueOnce(new ApiError(503, "down"));
  rerender(tree({ ...base, project_roots: ["/home/u/code"] }));
  const stale = await screen.findByTestId("discovery-stale");
  expect(stale).toHaveTextContent(/last one read/);
  expect(screen.getByRole("switch", { name: /~\/alpha/i })).toBeInTheDocument();
  vi.mocked(api.folders).mockResolvedValueOnce({ folders: [{ cwd: "/home/u/code/beta", label: "Beta" }] });
  await userEvent.click(within(stale).getByRole("button", { name: /retry/i }));
  expect(await screen.findByRole("switch", { name: /~\/code\/beta/i })).toBeInTheDocument();
  expect(screen.queryByTestId("discovery-stale")).toBeNull();
});

test("a refresh that fails after an EMPTY read says so with Retry — never 'No folders discovered yet.'", async () => {
  const { rerender } = render(tree({ ...base, project_roots: [] }));
  expect(await screen.findByText(/No folders discovered yet/)).toBeInTheDocument();
  vi.mocked(api.folders).mockRejectedValueOnce(new ApiError(503, "down"));
  rerender(tree({ ...base, project_roots: ["/home/u/code"] }));
  expect(await screen.findByTestId("discovery-read-failed")).toBeInTheDocument();
  expect(screen.queryByText(/No folders discovered yet/)).toBeNull();
});

test("a discovery read that fails says so with Retry — never 'No folders discovered yet.'", async () => {
  vi.mocked(api.folders).mockRejectedValueOnce(new ApiError(503, "down"));
  render(tree(base));
  const failed = await screen.findByTestId("discovery-read-failed");
  expect(failed).toHaveTextContent(/still save/);
  expect(screen.queryByText(/No folders discovered yet/)).toBeNull();
  vi.mocked(api.folders).mockResolvedValue({ folders: [{ cwd: "/home/u/alpha", label: "Alpha" }] });
  await userEvent.click(within(failed).getByRole("button", { name: /retry/i }));
  expect(await screen.findByRole("switch", { name: /~\/alpha/i })).toBeInTheDocument();
});
