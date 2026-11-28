import { expect, test } from "vitest";
import { sessionsUrl } from "./api";
import {
  EMPTY_SELECTION,
  addsPick,
  clusterDissolved,
  clusterMerged,
  forMission,
  parentLookup,
  pickMissionProject,
  pickUnclustered,
  removePick,
  selectionOf,
  selectionPatch,
  selectionQuery,
  soleCluster,
  toggleCluster,
  toggleProject,
  widenClusters,
} from "./scopeSelection";

// p-a holds clusters a1, a2; p-b holds b1 (#2438).
const parentOf = parentLookup([
  { id: "tc_a1", project_id: "p-a" },
  { id: "tc_a2", project_id: "p-a" },
  { id: "tc_b1", project_id: "p-b" },
]);
const sel = (projects: string[], clusters: string[] = [], unclustered = false) => ({
  projects,
  clusters,
  unclustered,
});

test("a state saved before #2438 reads as one tag; the arrays win once written", () => {
  expect(selectionOf({ project: "p-a" })).toEqual(sel(["p-a"]));
  // The tree's old pair meant "that cluster".
  expect(selectionOf({ project: "p-a", cluster: "tc_a1" })).toEqual(sel([], ["tc_a1"]));
  expect(selectionOf({ project: "p-a", cluster: "none" })).toEqual(sel(["p-a"], [], true));
  expect(selectionOf({})).toEqual(EMPTY_SELECTION);
  // Once arrays are written, a stale scalar can never come back.
  expect(selectionOf({ project: "p-a", projects: [], clusters: [] })).toEqual(EMPTY_SELECTION);
});

test("every write clears the old scalars, so the last chip removed leaves no selection", () => {
  const before = { project: "p-a" };
  const after = selectionPatch(removePick(selectionOf(before), "p-a"));
  expect(after).toEqual({ projects: [], clusters: [], project: "", cluster: "" });
  expect(selectionOf({ ...before, ...after })).toEqual(EMPTY_SELECTION);
  expect(selectionPatch(sel(["p-a"], [], true)).cluster).toBe("none");
});

test("a cluster of a picked project narrows it; a project drops its own clusters (widen)", () => {
  expect(toggleCluster(sel(["p-a", "p-b"]), "tc_a1", parentOf)).toEqual(sel(["p-b"], ["tc_a1"]));
  expect(toggleProject(sel([], ["tc_a1", "tc_a2", "tc_b1"]), "p-a", parentOf)).toEqual(
    sel(["p-a"], ["tc_b1"]),
  );
  // Toggling again unpicks.
  expect(toggleProject(sel(["p-a"]), "p-a", parentOf)).toEqual(sel([]));
  expect(toggleCluster(sel([], ["tc_a1"]), "tc_a1", parentOf)).toEqual(sel([]));
  // Any tag replaces "Not clustered".
  expect(toggleProject(sel(["p-a"], [], true), "p-b", parentOf)).toEqual(sel(["p-b"]));
  expect(toggleCluster(sel(["p-a"], [], true), "tc_b1", parentOf)).toEqual(sel([], ["tc_b1"]));
});

test("single choices: Not clustered, a mission node; the mission dropdown widens clusters", () => {
  expect(pickUnclustered("p-a")).toEqual(sel(["p-a"], [], true));
  expect(pickMissionProject("p-b")).toEqual(sel(["p-b"]));
  expect(forMission(sel(["p-b"], ["tc_a1", "tc_a2"]), parentOf)).toEqual(sel(["p-b", "p-a"]));
  // An unknown parent falls back to the saved scalar project, else goes.
  expect(forMission(sel([], ["tc_gone"]), parentOf, "p-x")).toEqual(sel(["p-x"]));
  expect(widenClusters(sel([], ["tc_gone"]), parentOf)).toEqual(sel([]));
  // "Not clustered" removed as one chip.
  expect(removePick(sel(["p-a"], [], true), "p-a")).toEqual(EMPTY_SELECTION);
});

test("the cluster head's merge and dissolve keep the pick true", () => {
  expect(clusterMerged(sel(["p-b"], ["tc_a1", "tc_a2"]), "tc_a1", "tc_a2")).toEqual(
    sel(["p-b"], ["tc_a2"]),
  );
  // Dissolved: its project takes its place ...
  expect(clusterDissolved(sel([], ["tc_a1"]), "tc_a1", parentOf)).toEqual(sel(["p-a"]));
  // ... unless a sibling cluster of that project is still picked (the project would only be
  // narrowed by it).
  expect(clusterDissolved(sel([], ["tc_a1", "tc_a2"]), "tc_a1", parentOf)).toEqual(
    sel([], ["tc_a2"]),
  );
});

test("the cluster head is for exactly one cluster pick and nothing else", () => {
  expect(soleCluster(sel([], ["tc_a1"]))).toBe("tc_a1");
  expect(soleCluster(sel(["p-b"], ["tc_a1"]))).toBeNull();
  expect(soleCluster(sel([], ["tc_a1", "tc_b1"]))).toBeNull();
  expect(soleCluster(sel(["p-a"], [], true))).toBeNull();
});

test("the query repeats project and cluster; Not clustered sends at most one project", () => {
  expect(selectionQuery(sel(["p-a", "p-b"], ["tc_b1"]))).toEqual({
    project: ["p-a", "p-b"],
    cluster: ["tc_b1"],
  });
  expect(selectionQuery(sel(["p-a"], [], true))).toEqual({ project: ["p-a"], cluster: ["none"] });
  const url = new URL(
    sessionsUrl({ project: ["p-a", "p-b"], cluster: ["tc_b1"] }),
    "https://x.example",
  );
  expect(url.searchParams.getAll("project")).toEqual(["p-a", "p-b"]);
  expect(url.searchParams.getAll("cluster")).toEqual(["tc_b1"]);
  // One value: exactly the URL a single pick produced before.
  expect(sessionsUrl({ project: "p-a" })).toContain("project=p-a");
  expect(new URL(sessionsUrl({}), "https://x.example").searchParams.has("project")).toBe(false);
});

test("a cluster pick records its project, so it widens without the cluster facets (#2438 review)", () => {
  const patch = selectionPatch(sel(["p-b"], ["tc_a1"]), parentOf);
  expect(patch.clusterParents).toEqual({ tc_a1: "p-a" });
  // No cluster facets (clusters off): the recorded parent answers.
  const recorded = parentLookup(undefined, patch.clusterParents);
  expect(recorded("tc_a1")).toBe("p-a");
  expect(widenClusters(selectionOf(patch), recorded)).toEqual(sel(["p-b", "p-a"]));
  // The facets win over a recorded parent; an unknown id stays unknown.
  expect(parentLookup([{ id: "tc_a1", project_id: "p-x" }], { tc_a1: "p-a" })("tc_a1")).toBe("p-x");
  expect(recorded("tc_zz")).toBeUndefined();
  expect(recorded("constructor")).toBeUndefined();
  // Without cluster picks nothing is recorded (the patch keeps its old shape).
  expect(selectionPatch(sel(["p-a"]), parentOf)).not.toHaveProperty("clusterParents");
});

test("adding a project or cluster tag is an add; a removal or the same pick is not (#2438 review)", () => {
  const msn = pickMissionProject("p-a");
  expect(addsPick(msn, toggleCluster(msn, "tc_a1", parentOf))).toBe(true);
  expect(addsPick(msn, toggleProject(msn, "p-b", parentOf))).toBe(true);
  expect(addsPick(msn, toggleProject(msn, "p-a", parentOf))).toBe(false);
  expect(addsPick(sel(["p-a"], ["tc_b1"]), removePick(sel(["p-a"], ["tc_b1"]), "tc_b1"))).toBe(false);
  expect(addsPick(msn, msn)).toBe(false);
  // Leaving "Not clustered" for its own project's tag adds that tag.
  expect(addsPick(pickUnclustered("p-a"), toggleProject(pickUnclustered("p-a"), "p-a", parentOf))).toBe(true);
});
