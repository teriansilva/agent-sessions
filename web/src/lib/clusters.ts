import { DEFAULT_PROJECT_ID } from "./overviewGraph";
import type { ProjectRef } from "../types/api";

/** The project a session's clusters belong to (#1997), as the list's project facet names it:
 *  the entity's id, or the synthetic Default project for a folder no project has adopted. The
 *  server decides the same way (`session_clusters.project_id_of`). */
export function clusterProjectId(ref: Pick<ProjectRef, "kind" | "id">): string {
  return ref.kind === "project" ? ref.id : DEFAULT_PROJECT_ID;
}

/** A cluster id, as opposed to "" (no filter) or `none` (in no cluster). */
export function isClusterId(value: string | undefined): value is string {
  return !!value && value.startsWith("tc_");
}

export const CLUSTER_TITLE_MAX = 60;
