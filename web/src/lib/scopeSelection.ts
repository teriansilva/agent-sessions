/** The sidebar's project/cluster field as a multiselect (#2438): ONE canonical selection and ONE
 *  reducer, shared by the tag field, the project tree, the mission dropdown and the cluster head.
 *
 *  The field is one facet whose picks OR together (the server's rule, `routes/sessions.py`): a
 *  session matches when it is in any chosen project or cluster. A chosen cluster narrows ITS OWN
 *  project, so the reducer never holds a project together with one of its clusters: picking a
 *  cluster of a chosen project replaces that project (narrow), picking a project drops its
 *  clusters (widen).
 *
 *  "Not clustered" (`cluster=none`) stays today's single, exclusive choice: at most one project,
 *  no cluster tags. Picking any tag drops it.
 *
 *  Old section state (before #2438) carries scalar `project` / `cluster` strings; `selectionOf`
 *  reads them as one tag, and every write clears them (`selectionPatch`), so removing the last chip
 *  can never bring an old saved selection back. */
import { isClusterId } from "./clusters";

export interface ScopeSelection {
  projects: string[];
  clusters: string[];
  /** "Not clustered" (`cluster=none`), with at most one project. */
  unclustered: boolean;
}

/** The part of the sidebar's `Filters` this module reads and writes. */
export interface ScopeFields {
  project?: string;
  cluster?: string;
  projects?: string[];
  clusters?: string[];
  /** Each cluster pick's project, recorded when it was picked, so the pick can still widen to
   *  its project when the cluster facets are gone (clusters turned off, the store unreadable). */
  clusterParents?: Record<string, string>;
}

export const EMPTY_SELECTION: ScopeSelection = { projects: [], clusters: [], unclustered: false };

/** The selection in force, from new arrays or (a state saved before #2438) the old scalars. */
export function selectionOf(f: ScopeFields): ScopeSelection {
  const scalar = f.cluster ?? "";
  const unclustered = scalar === "none";
  if (f.projects !== undefined || f.clusters !== undefined) {
    return { projects: f.projects ?? [], clusters: f.clusters ?? [], unclustered };
  }
  // The tree's old pair `project + cluster` meant "that cluster": one cluster tag.
  if (isClusterId(scalar)) return { projects: [], clusters: [scalar], unclustered: false };
  return { projects: f.project ? [f.project] : [], clusters: [], unclustered };
}

/** The `Filters` patch that stores ``sel``: the arrays, and the scalars cleared (`cluster` keeps
 *  only the exclusive `none`). With cluster picks, each one's project as ``parentOf`` names it is
 *  recorded beside them (`clusterParents`), so it outlives the cluster facets. */
export function selectionPatch(
  sel: ScopeSelection,
  parentOf?: ParentOf,
): Required<Omit<ScopeFields, "clusterParents">> & Pick<ScopeFields, "clusterParents"> {
  const patch = {
    projects: [...sel.projects],
    clusters: [...sel.clusters],
    project: "",
    cluster: sel.unclustered ? "none" : "",
  };
  if (!sel.clusters.length) return patch;
  const parents: Record<string, string> = {};
  for (const c of sel.clusters) {
    const p = parentOf?.(c);
    if (p) parents[c] = p;
  }
  return { ...patch, clusterParents: parents };
}

/** The `/api/sessions` values for ``sel``: repeated `project` and `cluster`. */
export function selectionQuery(sel: ScopeSelection): { project: string[]; cluster: string[] } {
  return {
    project: sel.unclustered ? sel.projects.slice(0, 1) : sel.projects,
    cluster: sel.unclustered ? ["none"] : sel.clusters,
  };
}

export function isEmptySelection(sel: ScopeSelection): boolean {
  return !sel.projects.length && !sel.clusters.length && !sel.unclustered;
}

/** A cluster's project, from the list's cluster facets. */
export type ParentOf = (clusterId: string) => string | undefined;

/** The `ParentOf` lookup over the list's cluster facets, then over the parents recorded with the
 *  picks (`clusterParents`): with the facets absent, a pick still knows its project. */
export function parentLookup(
  clusters: readonly { id: string; project_id: string }[] | undefined,
  recorded?: Readonly<Record<string, string>>,
): ParentOf {
  const map = new Map((clusters ?? []).map((c) => [c.id, c.project_id]));
  return (id) => map.get(id) ?? (recorded && Object.hasOwn(recorded, id) ? recorded[id] : undefined);
}

const without = (xs: string[], x: string) => xs.filter((y) => y !== x);

/** Toggle a project tag. Adding it drops its own cluster tags (widen) and "Not clustered". */
export function toggleProject(sel: ScopeSelection, id: string, parentOf: ParentOf): ScopeSelection {
  if (!sel.unclustered && sel.projects.includes(id)) {
    return { ...sel, projects: without(sel.projects, id) };
  }
  return {
    projects: sel.unclustered ? [id] : [...without(sel.projects, id), id],
    clusters: sel.unclustered ? [] : sel.clusters.filter((c) => parentOf(c) !== id),
    unclustered: false,
  };
}

/** Toggle a cluster tag. Adding it drops its own project's tag (narrow) and "Not clustered". */
export function toggleCluster(sel: ScopeSelection, id: string, parentOf: ParentOf): ScopeSelection {
  if (!sel.unclustered && sel.clusters.includes(id)) {
    return { ...sel, clusters: without(sel.clusters, id) };
  }
  const parent = parentOf(id);
  return {
    projects: sel.unclustered ? [] : sel.projects.filter((p) => p !== parent),
    clusters: sel.unclustered ? [id] : [...without(sel.clusters, id), id],
    unclustered: false,
  };
}

/** A chip's ×: removes that pick, whichever kind it is. "Not clustered" is one chip (with its
 *  project, if any), so its × clears the whole choice. */
export function removePick(sel: ScopeSelection, id: string): ScopeSelection {
  if (sel.unclustered) return EMPTY_SELECTION;
  return { projects: without(sel.projects, id), clusters: without(sel.clusters, id), unclustered: false };
}

/** True when ``next`` holds a project or cluster tag that ``prev`` did not: a tag was ADDED.
 *  Adding a tag clears a mission single choice (#2438), from the tree and the tag field alike: a
 *  session a mission holds is never in a cluster, so mission ∧ cluster could only be empty. A
 *  removal leaves the mission alone. */
export function addsPick(prev: ScopeSelection, next: ScopeSelection): boolean {
  const had = (ids: string[], id: string) => !prev.unclustered && ids.includes(id);
  return (
    next.projects.some((id) => !had(prev.projects, id)) ||
    next.clusters.some((id) => !had(prev.clusters, id))
  );
}

/** "Not clustered" under ``project`` (or across every project): the exclusive single choice. */
export function pickUnclustered(project?: string): ScopeSelection {
  return { projects: project ? [project] : [], clusters: [], unclustered: true };
}

/** A tree mission node: that project alone (its mission is set beside it, ANDed). */
export function pickMissionProject(project: string): ScopeSelection {
  return { projects: [project], clusters: [], unclustered: false };
}

/** Every cluster pick replaced by its project, so dropping the clusters keeps the scope true
 *  ("alpha › Login flow" becomes "alpha", never "everything"). A parent the facets cannot name
 *  falls back to ``fallbackProject`` (a state saved before #2438 kept its project in the scalar
 *  `project`), else that pick goes. "Not clustered" keeps its project. */
export function widenClusters(
  sel: ScopeSelection,
  parentOf: ParentOf,
  fallbackProject?: string,
): ScopeSelection {
  const parents = sel.clusters.map((c) => parentOf(c) ?? fallbackProject);
  const projects = [...new Set([...sel.projects, ...parents.filter((p): p is string => !!p)])];
  return { projects, clusters: [], unclustered: false };
}

/** The mission dropdown: a session a mission holds is never in a cluster, so the cluster picks
 *  (and "Not clustered") give way to their projects; the project picks stay. */
export function forMission(
  sel: ScopeSelection,
  parentOf: ParentOf,
  fallbackProject?: string,
): ScopeSelection {
  return widenClusters(sel, parentOf, fallbackProject);
}

/** The cluster head merged ``from`` into ``to``: the tag follows its sessions. */
export function clusterMerged(sel: ScopeSelection, from: string, to: string): ScopeSelection {
  const clusters = sel.clusters.map((c) => (c === from ? to : c));
  return { ...sel, clusters: [...new Set(clusters)] };
}

/** The cluster head dissolved ``id``: its project takes the tag's place, unless another cluster of
 *  that project is still picked (a project tag beside its own cluster would only be narrowed). */
export function clusterDissolved(
  sel: ScopeSelection,
  id: string,
  parentOf: ParentOf,
): ScopeSelection {
  const parent = parentOf(id);
  const clusters = without(sel.clusters, id);
  const sibling = clusters.some((c) => parentOf(c) === parent);
  const projects =
    parent && !sibling && !sel.projects.includes(parent) ? [...sel.projects, parent] : sel.projects;
  return { projects, clusters, unclustered: false };
}

/** The one cluster whose head the list shows: exactly one cluster tag and nothing else. */
export function soleCluster(sel: ScopeSelection): string | null {
  return !sel.unclustered && !sel.projects.length && sel.clusters.length === 1
    ? sel.clusters[0]
    : null;
}
