import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type NodeProps, ReactFlowProvider } from "@xyflow/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { OverviewActionsCtx } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return { ...actual, api: { ...actual.api, createProject: vi.fn() } };
});

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ReactFlowProvider supplies the store the node's <Handle> elements (hierarchy edges, #148)
// require when mounted in isolation.
const renderGroup = (data: object, refetchSessions: () => void = () => {}) =>
  render(
    <OverviewActionsCtx.Provider value={{ refetchSessions }}>
      <ReactFlowProvider>
        <ProjectGroupNode {...({ data } as unknown as NodeProps)} />
      </ReactFlowProvider>
    </OverviewActionsCtx.Provider>,
  );

// The header is presentational — collapse is toggled via the canvas's React Flow onNodeClick
// (#149). Here we verify it reflects collapsed state for assistive tech + shows name/count.
test("collapsed cluster header reports aria-expanded=false + the name/count", () => {
  renderGroup({
    project: "one",
    kind: "folder",
    cwd: "/home/u/one",
    count: 2,
    collapsed: true,
  });
  expect(screen.getByTitle(/expand \/home\/u\/one/i)).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  expect(screen.getByText("~/one")).toBeInTheDocument();
  expect(screen.getByText("2 sessions")).toBeInTheDocument();
});

test("expanded cluster header reports aria-expanded=true", () => {
  renderGroup({
    project: "one",
    kind: "folder",
    cwd: "/home/u/one",
    count: 1,
    collapsed: false,
  });
  expect(screen.getByTitle(/collapse \/home\/u\/one/i)).toHaveAttribute(
    "aria-expanded",
    "true",
  );
});

test("a custom name is shown with the path as a subtitle (#148)", () => {
  renderGroup({
    project: "one",
    kind: "folder",
    cwd: "/home/u/one",
    count: 1,
    collapsed: true,
    name: "My One",
  });
  expect(screen.getByText("My One")).toBeInTheDocument();
  expect(screen.getByText("~/one")).toBeInTheDocument(); // path subtitle for disambiguation
});

test("a project-entity group is labelled by the entity name with the path subtitle (#361)", () => {
  renderGroup({
    project: "Side",
    kind: "project",
    cwd: "/home/u/one",
    cwdCount: 1,
    count: 1,
    collapsed: true,
  });
  expect(screen.getByText("Side")).toBeInTheDocument();
  expect(screen.getByText("~/one")).toBeInTheDocument(); // path subtitle for disambiguation
});

// ---- #361 Phase 4: merged entity clusters + promote-to-project -----------------

test("an entity spanning several folders shows the folder count as subtitle", () => {
  renderGroup({
    project: "Side",
    kind: "project",
    cwd: "/home/u/app",
    cwdCount: 3,
    count: 5,
    collapsed: true,
  });
  expect(screen.getByText("Side")).toBeInTheDocument();
  expect(screen.getByText("3 folders")).toBeInTheDocument();
});

test("an entity color renders the header dot", () => {
  const { container } = renderGroup({
    project: "Side",
    kind: "project",
    cwd: "/home/u/app",
    cwdCount: 1,
    count: 1,
    collapsed: true,
    color: "#5fd7ff",
  });
  expect(container.querySelector(".tr-ov-proj-dot")).toBeTruthy();
});

// #285: the group publishes its colour as the --proj var; the tinted top border + dot are
// CSS consumers of it. A colourless group (the synthetic Default) gets neither layer.
test("the group colour propagates as --proj + the tinted class (#285)", () => {
  const { container } = renderGroup({
    project: "Side",
    kind: "project",
    cwd: "/home/u/app",
    cwdCount: 1,
    count: 1,
    collapsed: true,
    color: "#5fd7ff",
  });
  const root = container.querySelector(".tr-ov-group") as HTMLElement;
  expect(root.classList.contains("tinted")).toBe(true);
  expect(root.style.getPropertyValue("--proj")).toBe("#5fd7ff");
});

test("a colourless group (Default catch-all) has no tint layer and no dot (#285)", () => {
  const { container } = renderGroup({
    project: "Default",
    kind: "project",
    cwd: "/home/u/app",
    cwdCount: 1,
    count: 1,
    collapsed: true,
  });
  const root = container.querySelector(".tr-ov-group") as HTMLElement;
  expect(root.classList.contains("tinted")).toBe(false);
  expect(root.style.getPropertyValue("--proj")).toBe("");
  expect(container.querySelector(".tr-ov-proj-dot")).toBeNull();
});

test("the owner badge publishes its colour as --owner (#445/#285)", () => {
  const { container } = renderGroup({
    project: "one",
    kind: "folder",
    cwd: "/home/u/one",
    cwdCount: 1,
    count: 1,
    collapsed: true,
    owner: { name: "Side", color: "#5fd7ff" },
  });
  const owner = container.querySelector(".tr-ov-owner") as HTMLElement;
  expect(owner).not.toBeNull();
  expect(owner.style.getPropertyValue("--owner")).toBe("#5fd7ff");
});

test("'Make this a project' POSTs {name: basename, folders: [cwd]} then refetches", async () => {
  const user = userEvent.setup();
  const refetch = vi.fn();
  vi.mocked(api.createProject).mockResolvedValue({
    id: "p-9",
    name: "one",
    color: "",
    folders: ["/home/u/one"],
    archived: false,
    created_at: 0,
  });
  renderGroup(
    {
      project: "one",
      kind: "folder",
      cwd: "/home/u/one",
      cwdCount: 1,
      count: 1,
      collapsed: true,
    },
    refetch,
  );
  await user.click(
    screen.getByRole("button", { name: /make ~\/one a project/i }),
  );
  expect(api.createProject).toHaveBeenCalledWith({
    name: "one",
    folders: ["/home/u/one"],
  });
  await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
});

test("a 409 surfaces the server's detail inline and skips the refetch", async () => {
  const user = userEvent.setup();
  const refetch = vi.fn();
  vi.mocked(api.createProject).mockRejectedValue(
    new ApiError(409, "folder already owned by Side"),
  );
  renderGroup(
    {
      project: "one",
      kind: "folder",
      cwd: "/home/u/one",
      cwdCount: 1,
      count: 1,
      collapsed: true,
    },
    refetch,
  );
  await user.click(
    screen.getByRole("button", { name: /make ~\/one a project/i }),
  );
  expect(
    await screen.findByText("folder already owned by Side"),
  ).toBeInTheDocument();
  expect(refetch).not.toHaveBeenCalled();
});

test("entity groups have no promote button (they already are projects)", () => {
  renderGroup({
    project: "Side",
    kind: "project",
    cwd: "/home/u/app",
    cwdCount: 1,
    count: 1,
    collapsed: true,
  });
  expect(screen.queryByRole("button", { name: /a project$/i })).toBeNull();
});
