import { ChevronRight, Crosshair } from "lucide-react";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import type { Filters } from "../../hooks/useSessionsList";
import { projectColor } from "../../lib/format";
import { isClusterId } from "../../lib/clusters";
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
  /** The AI endpoint is set, so HQ can group sessions. */
  configured: boolean;
  /** The server could not apply the cluster filter (a store could not be read). */
  filterUnavailable?: boolean;
  onChange: (patch: Partial<Filters>) => void;
}

/** Device-local, like the Filters fold: whether the tree is open on THIS screen. */
export const CLUSTERS_OPEN_KEY = "tr-sidebar-clusters-open";

function readOpen(): boolean {
  try {
    return localStorage.getItem(CLUSTERS_OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

const count = (n: number | undefined) => (n == null ? "" : String(n));

/** Projects & clusters (#1997): project → its clusters, its missions and "Not clustered".
 *  Selecting a node filters the list through the same `sessions.filters` state as the Filters
 *  block, so the two can never disagree. Built from the list's facets only: every count is over
 *  the same scoped set the list pages through.
 *
 *  A plain nested list of buttons, not an ARIA tree: each node is one press, the selected one is
 *  `aria-current`, and a project's own chevron is a separate `aria-expanded` button, so it works
 *  with Tab and a screen reader's ordinary button navigation. */
export function ClusterTree({
  filters,
  facets,
  configured,
  filterUnavailable,
  onChange,
}: Props) {
  const [open, setOpen] = useState(readOpen);
  const bodyId = useId();
  // Projects the operator folded or unfolded by hand. A project that holds the selection is
  // open unless it was folded explicitly.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = () => {
    const next = !open;
    setOpen(next);
    try {
      localStorage.setItem(CLUSTERS_OPEN_KEY, next ? "1" : "0");
    } catch {
      /* private mode: the choice lasts for this page only */
    }
  };

  const cluster = filters.cluster ?? "";
  const mission = filters.mission ?? "";
  const known = facets.clusters !== undefined;
  const all = !filters.project && !cluster && !mission;
  const total = facets.projects.reduce((n, p) => n + (p.count ?? 0), 0);
  const select = (patch: Partial<Filters>) =>
    onChange({ project: "", cluster: "", mission: "", ...patch });
  const selectedTitle = isClusterId(cluster)
    ? facets.clusters?.find((c) => c.id === cluster)?.title
    : undefined;

  return (
    <div className={styles.tree} data-testid="cluster-tree">
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={toggle}
        data-testid="cluster-tree-toggle"
      >
        <ChevronRight size={14} aria-hidden="true" className={styles.chev} />
        <span>Projects &amp; clusters</span>
        <small className={selectedTitle ? styles.summaryOn : undefined}>
          {selectedTitle ?? (configured ? "By HQ" : "Off")}
        </small>
      </button>
      <div id={bodyId} className={styles.body} hidden={!open}>
        <div className={styles.bodyInner}>
          {!configured ? (
            <p className={styles.note} data-testid="cluster-tree-unconfigured">
              Clusters need an AI endpoint. Set one in{" "}
              <Link to={settingsPath("ai-endpoint")}>Settings → AI</Link>, or turn
              clusters off{" "}
              <Link to={settingsPath("ai-clusters")}>there</Link> to hide this.
            </p>
          ) : null}
          {configured && !known ? (
            <p className={styles.note} role="status">
              Clusters are unavailable right now. The projects below still filter the list.
            </p>
          ) : null}
          {filterUnavailable ? (
            <p className={styles.note} role="status">
              The cluster filter could not be applied — clusters are unavailable.
            </p>
          ) : null}
          <ul className={styles.nodes} aria-label="Projects and clusters">
            <li>
              <button
                type="button"
                className={styles.node}
                aria-current={all ? "true" : undefined}
                onClick={() => select({})}
              >
                <span className={styles.name}>All sessions</span>
                <span className={styles.count}>{count(total)}</span>
              </button>
            </li>
            {facets.projects.map((p) => {
              const clusters = (facets.clusters ?? []).filter(
                (c) => c.project_id === p.id,
              );
              const missions = (facets.missions ?? []).filter(
                (m) => (m.by_project?.[p.id] ?? 0) > 0,
              );
              const grouped =
                clusters.reduce((n, c) => n + c.count, 0) +
                missions.reduce((n, m) => n + (m.by_project?.[p.id] ?? 0), 0);
              const rest = Math.max(0, (p.count ?? 0) - grouped);
              const holdsSelection = filters.project === p.id;
              const isOpen = expanded[p.id] ?? holdsSelection;
              const hasChildren = known && (clusters.length > 0 || missions.length > 0);
              const childrenId = `${bodyId}-${p.id}`;
              return (
                <li key={p.id}>
                  <div className={styles.projectRow}>
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
                    <button
                      type="button"
                      className={`${styles.node} ${styles.project}`}
                      aria-current={
                        holdsSelection && !cluster && !mission ? "true" : undefined
                      }
                      onClick={() => {
                        setExpanded((e) => ({ ...e, [p.id]: true }));
                        select({ project: p.id });
                      }}
                    >
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
                          aria-current={cluster === c.id ? "true" : undefined}
                          onClick={() => select({ project: p.id, cluster: c.id })}
                          data-testid="cluster-node"
                        >
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
                            holdsSelection && mission === m.id ? "true" : undefined
                          }
                          onClick={() => select({ project: p.id, mission: m.id })}
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
                            holdsSelection && cluster === "none" ? "true" : undefined
                          }
                          onClick={() => select({ project: p.id, cluster: "none" })}
                          data-testid="cluster-none-node"
                        >
                          <span className={`${styles.bullet} ${styles.bulletOpen}`} aria-hidden="true" />
                          <span className={styles.name}>Not clustered</span>
                          <span className={styles.count}>{rest}</span>
                        </button>
                      </li>
                    ) : null}
                    {known && configured && !hasChildren ? (
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
      </div>
    </div>
  );
}
