import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { NodeProps } from "@xyflow/react";
import { expect, test, vi } from "vitest";
import { OverviewActions } from "./overviewActions";
import { ProjectGroupNode } from "./ProjectGroupNode";

function renderGroup(data: object, toggle = vi.fn()) {
  render(
    <OverviewActions.Provider value={{ toggle }}>
      <ProjectGroupNode {...({ data } as unknown as NodeProps)} />
    </OverviewActions.Provider>,
  );
  return toggle;
}

test("the cluster header is a button that toggles via the actions context (#144)", async () => {
  const toggle = renderGroup({ project: "one", cwd: "/p/one", count: 2, collapsed: true });
  const btn = screen.getByRole("button");
  expect(btn).toHaveAttribute("aria-expanded", "false"); // collapsed
  await userEvent.click(btn);
  expect(toggle).toHaveBeenCalledWith("/p/one");
});

test("an expanded cluster reports aria-expanded=true", () => {
  renderGroup({ project: "one", cwd: "/p/one", count: 1, collapsed: false });
  expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
});
