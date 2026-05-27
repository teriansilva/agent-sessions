import { render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { expect, test } from "vitest";
import { ProjectGroupNode } from "./ProjectGroupNode";

const renderGroup = (data: object) =>
  render(<ProjectGroupNode {...({ data } as unknown as NodeProps)} />);

// The header is presentational — collapse is toggled via the canvas's React Flow onNodeClick
// (#149). Here we verify it reflects collapsed state for assistive tech + shows path/count.
test("collapsed cluster header reports aria-expanded=false + the path/count", () => {
  renderGroup({ project: "one", cwd: "/home/u/one", count: 2, collapsed: true });
  expect(screen.getByTitle(/expand \/home\/u\/one/i)).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("~/one")).toBeInTheDocument();
  expect(screen.getByText("2 sessions")).toBeInTheDocument();
});

test("expanded cluster header reports aria-expanded=true", () => {
  renderGroup({ project: "one", cwd: "/home/u/one", count: 1, collapsed: false });
  expect(screen.getByTitle(/collapse \/home\/u\/one/i)).toHaveAttribute("aria-expanded", "true");
});
