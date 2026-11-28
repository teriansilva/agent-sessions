import { Check, ChevronRight, Crosshair } from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import type { Filters } from "../../hooks/useSessionsList";
import { projectColor } from "../../lib/format";
import {
  addsPick,
  EMPTY_SELECTION,
  isEmptySelection,
  pickMissionProject,
  pickUnclustered,
  selectionOf,
  parentLookup,
  selectionPatch,
  toggleCluster,
  toggleProject,
  type ScopeSelection,
} from "../../lib/scopeSelection";
import { settingsPath } from "../../routes/settingsTabs";
import type {
  ProjectRef,
  SessionClusterFacet,
  SessionMissionFacet,
} from "../../types/api";
import styles from "./Clusters.module.css";

interface Props {
  filters: Filters;
  facets: {
    projects: ProjectRef[];
    missions?: SessionMissionFacet[];
    clusters?: SessionClusterFacet[];
  };
  /** Session clusters are on (#1997). Off, the tree lists projects and their missions. */
  clusters: boolean;
  /** The AI endpoint is set, so HQ can group sessions. */
  configured: boolean;
  /** The server could not apply the cluster filter (a store could not be read). */
  filterUnavailable?: boolean;
  onChange: (patch: Partial<Filters>) => void;
}

const count = (n: number | undefined) => (n == null ? "" : String(n));

/** The project tree (#1997; inside the Filters fold since #2187): All sessions, then each project
 *  with its clusters (when clusters are on), its missions and "Not clustered". It is THE project
 *  filter: the sidebar has no project dropdown. Since #2438 a project or cluster node toggles a
 *  tag of the multiselect (`lib/scopeSelection.ts`, shared with the tag field); a mission node and
 *  "Not clustered" stay single choices. Built
 *  from the list's facets only: every count is over the same scoped set the list pages through.
 *
 *  A plain nested list of buttons, not an ARIA tree: each node is one press, the selected one is
 *  `aria-current`, and a project's own chevron is a separate `aria-expanded` button, so it works
 *  with Tab and a screen reader's ordinary button navigation. */
export function ProjectTree({
  filters,
  facets,
  clusters: clustersOn,
  configured,
  filterUnavailable,
  onChange,
}: Props) {
  const bodyId = useId();
  // Projects the operator folded or unfolded by hand. A project that holds the selection is
  // open unless it was folded explicitly.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const sel = selectionOf(filters);
  const mission = filters.mission ?? "";
  // Clusters off: the tree claims nothing about them (no cluster nodes, no "Not clustered").
  const known = clustersOn && facets.clusters !== undefined;
  const all = isEmptySelection(sel) && !mission;
  const total = facets.projects.reduce((n, p) => n + (p.count ?? 0), 0);
  const parentOf = parentLookup(facets.clusters, filters.clusterParents);
  // Every pick goes through the one reducer (#2438). A project or cluster node TOGGLES its tag;
  // ADDING one clears a mission node's single choice (`addsPick`), removing one leaves it. "All
  // sessions", a mission node and "Not clustered" are single choices that replace the whole pick.
  const apply = (next: ScopeSelection, patch: Partial<Filters> = {}) =>
    onChange({
      ...selectionPatch(next, parentOf),
      ...(mission && addsPick(sel, next) ? { mission: "" } : {}),
      ...patch,
    });
  const pressed = (on: boolean) => (on ? "true" : "false");

  return (
    <div className={styles.tree} data-testid="project-tree">
      {clustersOn && !configured ? (
        <p className={styles.note} data-testid="cluster-tree-unconfigured">
          Clusters need an AI endpoint. Set one in{" "}
          <Link to={settingsPath("ai-endpoint")}>Settings → AI</Link>, or turn
          clusters off{" "}
          <Link to={settingsPath("ai-clusters")}>there</Link>.
        </p>
      ) : null}
      {clustersOn && configured && !known ? (
        <p className={styles.note} role="status">
          Clusters are unavailable right now. The projects below still filter the list.
        </p>
      ) : null}
      {filterUnavailable ? (
        <p className={styles.note} role="status">
          The cluster filter could not be applied — clusters are unavailable.
        </p>
      ) : null}
      <ul className={styles.nodes} aria-label="Projects">
        <li>
          <button
            type="button"
            className={styles.node}
            aria-current={all ? "true" : undefined}
            onClick={() => apply(EMPTY_SELECTION, { mission: "" })}
          >
            <span className={styles.name}>All sessions</span>
            <span className={styles.count}>{count(total)}</span>
          </button>
        </li>
        {facets.projects.map((p) => {
          const clusters = (known ? (facets.clusters ?? []) : []).filter(
            (c) => c.project_id === p.id,
          );
          const missions = (facets.missions ?? []).filter(
            (m) => (m.by_project?.[p.id] ?? 0) > 0,
          );
          const grouped =
            clusters.reduce((n, c) => n + c.count, 0) +
            missions.reduce((n, m) => n + (m.by_project?.[p.id] ?? 0), 0);
          const rest = Math.max(0, (p.count ?? 0) - grouped);
          const projectPicked = !sel.unclustered && sel.projects.includes(p.id);
          const holdsSelection =
            sel.projects.includes(p.id) || sel.clusters.some((c) => parentOf(c) === p.id);
          const isOpen = expanded[p.id] ?? holdsSelection;
          // With clusters known every project has "Not clustered"; off, only its missions.
          const hasChildren = known || missions.length > 0;
          const childrenId = `${bodyId}-${p.id}`;
          return (
            <li key={p.id}>
              <div className={styles.projectRow}>
                {/* A project with nothing under it has no fold: a spacer keeps the swatches in line. */}
                {hasChildren ? (
                  <button
                    type="button"
                    className={styles.fold}
                    aria-expanded={isOpen}
                    aria-controls={childrenId}
                    aria-label={`${isOpen ? "Fold" : "Unfold"} ${p.name}`}
                    onClick={() =>
                      setExpanded((e) => ({ ...e, [p.id]: !isOpen }))
                    }
                  >
                    <ChevronRight size={12} aria-hidden="true" className={styles.chev} />
                  </button>
                ) : (
                  <span className={styles.foldSpacer} aria-hidden="true" />
                )}
                <button
                  type="button"
                  className={`${styles.node} ${styles.project}`}
                  aria-pressed={pressed(projectPicked)}
                  onClick={() => {
                    setExpanded((e) => ({ ...e, [p.id]: true }));
                    apply(toggleProject(sel, p.id, parentOf));
                  }}
                  data-testid="project-node"
                >
                  <span className={styles.tick} aria-hidden="true">
                    {projectPicked ? <Check size={11} /> : null}
                  </span>
                  <span
                    className={styles.swatch}
                    style={{ background: p.color || projectColor(p.id) }}
                    aria-hidden="true"
                  />
                  <span className={styles.name}>{p.name}</span>
                  <span className={styles.count}>{count(p.count)}</span>
                </button>
              </div>
              <ul id={childrenId} className={styles.children} hidden={!isOpen}>
                {clusters.map((c) => (
                  <li key={c.id}>
                    <button
                      type="button"
                      className={`${styles.node} ${styles.child}`}
                      aria-pressed={pressed(!sel.unclustered && sel.clusters.includes(c.id))}
                      onClick={() => apply(toggleCluster(sel, c.id, parentOf))}
                      data-testid="cluster-node"
                    >
                      <span className={styles.tick} aria-hidden="true">
                        {!sel.unclustered && sel.clusters.includes(c.id) ? <Check size={11} /> : null}
                      </span>
                      <span className={styles.bullet} aria-hidden="true" />
                      <span className={styles.name}>{c.title}</span>
                      {c.kept ? (
                        <span className={styles.kept} title="You chose this title; HQ no longer renames it">
                          Kept
                        </span>
                      ) : null}
                      <span className={styles.count}>{c.count}</span>
                    </button>
                  </li>
                ))}
                {missions.map((m) => (
                  <li key={m.id}>
                    <button
                      type="button"
                      className={`${styles.node} ${styles.child}`}
                      aria-current={
                        mission === m.id &&
                        !sel.unclustered &&
                        !sel.clusters.length &&
                        sel.projects.length === 1 &&
                        sel.projects[0] === p.id
                          ? "true"
                          : undefined
                      }
                      onClick={() => apply(pickMissionProject(p.id), { mission: m.id })}
                      data-testid="cluster-mission-node"
                    >
                      <Crosshair size={10} aria-hidden="true" className={styles.missionMark} />
                      <span className={styles.name}>
                        <span className={styles.kind}>Mission</span>
                        {" · "}
                        {m.title}
                      </span>
                      <span className={styles.count}>{m.by_project?.[p.id]}</span>
                    </button>
                  </li>
                ))}
                {known ? (
                  <li>
                    <button
                      type="button"
                      className={`${styles.node} ${styles.child}`}
                      aria-current={
                        sel.unclustered && sel.projects[0] === p.id ? "true" : undefined
                      }
                      onClick={() => apply(pickUnclustered(p.id), { mission: "" })}
                      data-testid="cluster-none-node"
                    >
                      <span className={`${styles.bullet} ${styles.bulletOpen}`} aria-hidden="true" />
                      <span className={styles.name}>Not clustered</span>
                      <span className={styles.count}>{rest}</span>
                    </button>
                  </li>
                ) : null}
                {known && configured && clusters.length === 0 && missions.length === 0 ? (
                  <li className={styles.note}>
                    A cluster needs two sessions on one topic. HQ groups this
                    project once it has them.
                  </li>
                ) : null}
              </ul>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
