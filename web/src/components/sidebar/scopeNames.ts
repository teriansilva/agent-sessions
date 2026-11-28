/** How the project/cluster picks are named (#2438), for the chips and the folded header alike. A
 *  pick the facets no longer list keeps an honest name, never a blank and never someone else's. */
import type { ProjectRef, SessionClusterFacet } from "../../types/api";

export interface ScopeFacets {
  projects: ProjectRef[];
  /** Absent when clusters are off or could not be read. */
  clusters?: SessionClusterFacet[];
}

/** A project filter the facets do not name still narrows the list, so it still gets a name: a
 *  saved folder path (the server accepts a legacy cwd as `project`; its facets list entities
 *  only) by its last segment, anything else (a project that has since gone) honestly. */
export function unlistedProject(id: string): string {
  if (id.startsWith("/")) return id.replace(/\/+$/, "").split("/").pop() || id;
  return "Project no longer there";
}

export function projectName(facets: ScopeFacets, id: string): string {
  return facets.projects.find((p) => p.id === id)?.name ?? unlistedProject(id);
}

/** A cluster's title. ``known`` is the last title seen for it (the tag field remembers them), so a
 *  chip keeps its name while the cluster store is unreadable. */
export function clusterName(facets: ScopeFacets, id: string, known?: string): string {
  if (facets.clusters === undefined) return known ? `${known} (unavailable)` : "Cluster (unavailable)";
  return facets.clusters.find((c) => c.id === id)?.title ?? "Cluster no longer there";
}

/** The project a cluster belongs to, by name, or "" when unknown. */
export function clusterProjectName(facets: ScopeFacets, id: string): string {
  const pid = facets.clusters?.find((c) => c.id === id)?.project_id;
  return pid ? (facets.projects.find((p) => p.id === pid)?.name ?? "") : "";
}
