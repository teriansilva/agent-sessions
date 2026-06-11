import { render, screen } from "@testing-library/react";
import { type NodeProps, ReactFlowProvider } from "@xyflow/react";
import { expect, test } from "vitest";
import { ProjectGroupNode } from "./ProjectGroupNode";

// ReactFlowProvider supplies the store the node's <Handle> elements (hierarchy edges, #148)
// require when mounted in isolation.
const renderGroup = (data: object) =>
  render(
    <ReactFlowProvider>
      <ProjectGroupNode {...({ data } as unknown as NodeProps)} />
    </ReactFlowProvider>,
  );

// The header is presentational — collapse is toggled via the canvas's React Flow onNodeClick
// (#149). Here we verify it reflects collapsed state for assistive tech + shows name/count.
test("collapsed cluster header reports aria-expanded=false + the name/count", () => {
  renderGroup({ project: "one", kind: "folder", cwd: "/home/u/one", count: 2, collapsed: true });
  expect(screen.getByTitle(/expand \/home\/u\/one/i)).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("~/one")).toBeInTheDocument();
  expect(screen.getByText("2 sessions")).toBeInTheDocument();
});

test("expanded cluster header reports aria-expanded=true", () => {
  renderGroup({ project: "one", kind: "folder", cwd: "/home/u/one", count: 1, collapsed: false });
  expect(screen.getByTitle(/collapse \/home\/u\/one/i)).toHaveAttribute("aria-expanded", "true");
});

test("a custom name is shown with the path as a subtitle (#148)", () => {
  renderGroup({ project: "one", kind: "folder", cwd: "/home/u/one", count: 1, collapsed: true, name: "My One" });
  expect(screen.getByText("My One")).toBeInTheDocument();
  expect(screen.getByText("~/one")).toBeInTheDocument(); // path subtitle for disambiguation
});

test("a project-entity group is labelled by the entity name with the path subtitle (#361)", () => {
  renderGroup({ project: "Side", kind: "project", cwd: "/home/u/one", count: 1, collapsed: true });
  expect(screen.getByText("Side")).toBeInTheDocument();
  expect(screen.getByText("~/one")).toBeInTheDocument(); // path subtitle for disambiguation
});
