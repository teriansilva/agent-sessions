import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import type { Filters } from "../../hooks/useSessionsList";
import { ProjectTree } from "./ProjectTree";

const EMPTY: Filters = { q: "", project: "", engine: "", mission: "", cluster: "", archived: false };
const TC = "tc_0a1b2c3d4e5f";
const FACETS = {
  projects: [
    { kind: "project" as const, id: "p-a", name: "alpha", count: 6 },
    { kind: "project" as const, id: "p-b", name: "beta", count: 1 },
  ],
  missions: [
    { id: "msn_1", title: "Release", state: "running", count: 3, by_project: { "p-a": 2, "p-b": 1 } },
  ],
  clusters: [
    { id: TC, title: "Login flow", kept: false, project_id: "p-a", count: 3 },
    { id: "tc_ffffffffffff", title: "Billing", kept: true, project_id: "p-x", count: 2 },
  ],
};

function tree(over: Partial<Parameters<typeof ProjectTree>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <MemoryRouter>
      <ProjectTree filters={EMPTY} facets={FACETS} clusters configured onChange={onChange} {...over} />
    </MemoryRouter>,
  );
  return onChange;
}

beforeEach(() => localStorage.clear());

test("a project lists its own clusters, its missions with their count in it, and the rest", async () => {
  tree();
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  const nodes = screen.getByRole("list", { name: "Projects" });
  expect(within(nodes).getByRole("button", { name: /Login flow\s*3/ })).toBeVisible();
  // The mission spans two projects: under alpha it counts alpha's two sessions.
  expect(within(nodes).getByRole("button", { name: /Mission · Release\s*2/ })).toBeVisible();
  // 6 in the project, 3 clustered, 2 in the mission.
  expect(within(nodes).getByRole("button", { name: /Not clustered\s*1/ })).toBeVisible();
  // Another project's cluster is not listed here.
  expect(within(nodes).queryByRole("button", { name: /Billing/ })).toBeNull();
  expect(within(nodes).getByRole("button", { name: /All sessions\s*7/ })).toBeVisible();
});

test("selecting a node replaces the project, cluster and mission filters together", async () => {
  const onChange = tree({ filters: { ...EMPTY, project: "p-a", mission: "msn_1" } });
  await userEvent.click(screen.getByRole("button", { name: /Login flow/ }));
  expect(onChange).toHaveBeenLastCalledWith({ project: "p-a", cluster: TC, mission: "" });
  await userEvent.click(screen.getByRole("button", { name: /Mission · Release/ }));
  expect(onChange).toHaveBeenLastCalledWith({ project: "p-a", cluster: "", mission: "msn_1" });
  await userEvent.click(screen.getByRole("button", { name: /Not clustered/ }));
  expect(onChange).toHaveBeenLastCalledWith({ project: "p-a", cluster: "none", mission: "" });
  await userEvent.click(screen.getByRole("button", { name: /All sessions/ }));
  expect(onChange).toHaveBeenLastCalledWith({ project: "", cluster: "", mission: "" });
});

test("the selected cluster is marked and its project is open", () => {
  tree({ filters: { ...EMPTY, project: "p-a", cluster: TC } });
  expect(screen.getByTestId("cluster-node")).toHaveAttribute("aria-current", "true");
  expect(screen.getByRole("button", { name: "Fold alpha" })).toHaveAttribute("aria-expanded", "true");
});

test("with clusters off it lists projects and their missions, and nothing about clusters (#2187)", async () => {
  tree({ clusters: false, configured: false });
  expect(screen.queryByTestId("cluster-tree-unconfigured")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  expect(screen.getByRole("button", { name: /Mission · Release\s*2/ })).toBeVisible();
  expect(screen.queryByTestId("cluster-node")).toBeNull();
  expect(screen.queryByTestId("cluster-none-node")).toBeNull();
  expect(screen.queryByText(/A cluster needs two sessions/)).toBeNull();
  // A project with nothing under it has no fold, and still filters.
  cleanup();
  const bare = { projects: [{ kind: "project" as const, id: "p-c", name: "gamma", count: 2 }] };
  const onChange = tree({ clusters: false, facets: bare });
  expect(screen.queryByRole("button", { name: /Unfold gamma/ })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /^gamma/ }));
  expect(onChange).toHaveBeenLastCalledWith({ project: "p-c", cluster: "", mission: "" });
});

test("with clusters off and no clusters facet at all, a project still lists its missions (#2187)", async () => {
  const { clusters: _omit, ...noClusters } = FACETS;
  void _omit;
  tree({ clusters: false, facets: noClusters });
  expect(screen.queryByText(/Clusters are unavailable/)).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Unfold alpha" }));
  expect(screen.getByRole("button", { name: /Mission · Release\s*2/ })).toBeVisible();
  expect(screen.queryByTestId("cluster-none-node")).toBeNull();
});

test("a kept cluster says so", async () => {
  tree({
    facets: { ...FACETS, clusters: [{ ...FACETS.clusters[0], kept: true }] },
    filters: { ...EMPTY, project: "p-a" },
  });
  expect(screen.getByRole("button", { name: /Login flow/ })).toHaveTextContent("Kept");
});

test("unknown clusters (no facet) offer projects only and say why", async () => {
  tree({ facets: { projects: FACETS.projects }, filters: { ...EMPTY, project: "p-a" } });
  expect(screen.getByText(/Clusters are unavailable right now/)).toBeVisible();
  expect(screen.queryByTestId("cluster-node")).toBeNull();
  // "Not clustered" would be a claim the server did not make.
  expect(screen.queryByTestId("cluster-none-node")).toBeNull();
  expect(screen.getByRole("button", { name: /alpha\s*6/ })).toBeVisible();
});

test("without an AI endpoint the tree says what it needs and links to Settings", () => {
  tree({ configured: false, facets: { ...FACETS, clusters: [] } });
  const note = screen.getByTestId("cluster-tree-unconfigured");
  expect(note).toHaveTextContent("Clusters need an AI endpoint");
  expect(within(note).getByRole("link", { name: "Settings → AI" })).toHaveAttribute(
    "href",
    "/settings/ai-endpoint",
  );
  expect(within(note).getByRole("link", { name: "there" })).toHaveAttribute(
    "href",
    "/settings/ai-clusters",
  );
});

test("a project with nothing to group says when HQ will", () => {
  tree({
    facets: { projects: FACETS.projects, missions: [], clusters: [] },
    filters: { ...EMPTY, project: "p-b" },
  });
  // One per project with nothing grouped; only the open project's is on screen.
  const notes = screen.getAllByText(/A cluster needs two sessions on one topic/);
  expect(notes.filter((n) => !n.closest("[hidden]"))).toHaveLength(1);
});

test("a failed cluster filter is said", () => {
  tree({ filterUnavailable: true });
  expect(screen.getByText(/The cluster filter could not be applied/)).toBeVisible();
});
