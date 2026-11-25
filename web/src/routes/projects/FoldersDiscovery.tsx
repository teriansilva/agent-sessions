/** HQ → Projects → Folders & discovery (#2143): what was Settings → Projects' Folder discovery and
 *  Session overview cards (#465, #174), moved unchanged in behaviour and rebuilt on the NIGHTJAR
 *  kit (`Panel tone="quiet"`, `Seg`, `Switch`, ghost compact plates).
 *
 *  - Project roots and folder exclusions (#465): a HARD discovery scope plus a manual list for
 *    scratch folders. Each list commits through `setPrefs` and re-reads `/api/config` (#470).
 *  - Overview visibility (#174, #335, #615): two modes (show all · only included); folders grouped
 *    under their owning project, an Unassigned group last. What switching a folder off DOES depends
 *    on adoption — it always withholds the folder as a launch location, and for an UNADOPTED folder
 *    also drops its sessions from the sidebar, the filter and the map (`sessions.py` `_visible`;
 *    pinned both ways by `tests/test_projects.py` and `overviewGraph.test.ts`). Only an unassigned
 *    folder can be renamed: an adopted one is named by its project.
 *
 *  A discovery read that fails says so in place: roots and exclusions above still save (they come
 *  from the config), and the folder list returns when discovery answers. Before #2143 a failed read
 *  showed "No folders discovered yet." — an empty list for an error. */
import { ArrowRight, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../../app/config";
import { useOverviewPrefs } from "../../app/overviewPrefs";
import { FolderPickerModal } from "../../components/FolderPickerModal";
import { Callsign, Panel } from "../../components/hud/Panel";
import { Seg, Switch } from "../../components/hud/Controls";
import action from "../../components/ui/actionButton.module.css";
import { api } from "../../lib/api";
import { shortCwd } from "../../lib/format";
import { buildProjectTree, flattenTree, owningProjectId } from "../../lib/projectTree";
import { projectPath } from "../../lib/routes";
import type { ProjectEntity } from "../../types/api";
import { RenameProjectModal } from "../RenameProjectModal";
import { ProjectSwatch, ReadFailed, StaleNotice } from "./ProjectBits";
import { readError, useDiscoveredFolders } from "./projectsData";
import styles from "./projects.module.css";

/** Content key over the effective discovery scope (#470). Changes exactly when the server-echoed
 *  `project_roots` / `folder_exclusions` in /api/config change, so the discovered-folder read can
 *  depend on it without re-running for unrelated config updates. */
function useDiscoveryKey(): string {
  const config = useConfig();
  const roots = config?.project_roots ?? [];
  const exclusions = config?.folder_exclusions ?? [];
  return `${roots.join("\n")}\u0000${exclusions.join("\n")}`;
}

export function FoldersDiscovery() {
  return (
    <div className={styles.cols}>
      <div className={styles.body}>
        <DiscoveryLists />
      </div>
      <OverviewVisibility />
    </div>
  );
}

/** Project roots + folder exclusions: optimistic local state seeded from config, reflecting
 *  external changes (another device, a reload) with React's render-phase "adjust state on change"
 *  pattern. `project_roots` echoes the EFFECTIVE (normalized, existing-dir-only) list. */
function DiscoveryLists() {
  const config = useConfig();
  const refreshConfig = useConfigRefresh();
  const configRoots = config?.project_roots ?? [];
  const configExclusions = config?.folder_exclusions ?? [];
  const [roots, setRoots] = useState<string[]>(configRoots);
  const [exclusions, setExclusions] = useState<string[]>(configExclusions);
  const [syncedRoots, setSyncedRoots] = useState(configRoots);
  const [syncedExclusions, setSyncedExclusions] = useState(configExclusions);
  const [error, setError] = useState<string | null>(null);
  // Compare by content so a fresh array identity from config doesn't churn local edits.
  if (configRoots.join("\n") !== syncedRoots.join("\n")) {
    setSyncedRoots(configRoots);
    setRoots(configRoots);
  }
  if (configExclusions.join("\n") !== syncedExclusions.join("\n")) {
    setSyncedExclusions(configExclusions);
    setExclusions(configExclusions);
  }
  const [picking, setPicking] = useState<{ kind: "root" | "exclusion"; trigger: HTMLElement | null } | null>(null);

  const commitRoots = (next: string[]) => {
    const prev = roots;
    setRoots(next);
    setError(null);
    api
      .setPrefs({ project_roots: next })
      .then((r) => {
        const eff = (r as { project_roots?: string[] }).project_roots;
        if (Array.isArray(eff)) setRoots(eff);
        refreshConfig();
      })
      .catch((e) => {
        setRoots(prev);
        setError(`${readError(e, "Couldn’t save the project roots")}.`);
      });
  };
  const commitExclusions = (next: string[]) => {
    const prev = exclusions;
    setExclusions(next);
    setError(null);
    api
      .setPrefs({ folder_exclusions: next })
      .then(() => refreshConfig())
      .catch((e) => {
        setExclusions(prev);
        setError(`${readError(e, "Couldn’t save the exclusions")}.`);
      });
  };
  const onPick = (path: string) => {
    if (picking?.kind === "root") {
      if (!roots.includes(path)) commitRoots([...roots, path]);
    } else if (picking?.kind === "exclusion") {
      if (!exclusions.includes(path)) commitExclusions([...exclusions, path]);
    }
    setPicking(null);
  };

  return (
    <>
      <Panel tone="quiet" className={styles.panel} aria-labelledby="discovery-h">
        <Callsign
          as="h2"
          id="discovery-h"
          title="Project roots"
          value={roots.length ? `${roots.length} root${roots.length === 1 ? "" : "s"}` : "unscoped"}
        />
        <p className={styles.text}>
          Discovery only looks inside these folders. It is a hard scope: folders outside a root are
          hidden from the sidebar, the filter and every picker too. With no roots, discovery is
          unscoped (every session’s folder plus ~/claude subfolders).
        </p>
        {roots.length === 0 ? (
          <p className={styles.quiet}>No roots — discovery is unscoped.</p>
        ) : (
          <ul className={styles.paths} aria-label="Root directories">
            {roots.map((r) => (
              <li key={r}>
                <span className={styles.path} title={r}>
                  {shortCwd(r)}
                </span>
                <button
                  type="button"
                  className={`${action.ghost} ${action.compact}`}
                  onClick={() => commitRoots(roots.filter((x) => x !== r))}
                  aria-label={`Remove root ${shortCwd(r)}`}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <div>
          <button
            type="button"
            className={action.ghost}
            onClick={(e) => setPicking({ kind: "root", trigger: e.currentTarget })}
          >
            <Plus size={14} aria-hidden="true" /> Add root…
          </button>
        </div>
      </Panel>

      <Panel tone="quiet" className={styles.panel} aria-labelledby="exclusions-h">
        <Callsign
          as="h2"
          id="exclusions-h"
          title="Folder exclusions"
          value={exclusions.length ? `${exclusions.length} folder${exclusions.length === 1 ? "" : "s"}` : "none"}
        />
        <p className={styles.text}>
          Scratch folders that slip through discovery. An excluded folder is never offered as a
          launch location and its sessions leave the sidebar.
        </p>
        {exclusions.length === 0 ? (
          <p className={styles.quiet}>No exclusions.</p>
        ) : (
          <ul className={styles.paths} aria-label="Excluded folders">
            {exclusions.map((x) => (
              <li key={x}>
                <span className={styles.path} title={x}>
                  {shortCwd(x)}
                </span>
                <button
                  type="button"
                  className={`${action.ghost} ${action.compact}`}
                  onClick={() => commitExclusions(exclusions.filter((e) => e !== x))}
                  aria-label={`Remove exclusion ${shortCwd(x)}`}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <div>
          <button
            type="button"
            className={action.ghost}
            onClick={(e) => setPicking({ kind: "exclusion", trigger: e.currentTarget })}
          >
            <Plus size={14} aria-hidden="true" /> Add exclusion…
          </button>
        </div>
      </Panel>
      {error && (
        <p className="nj-error" role="alert">
          {error}
        </p>
      )}
      {picking && (
        <FolderPickerModal
          title={picking.kind === "root" ? "Choose a root directory" : "Choose a folder to exclude"}
          onPick={onPick}
          onCancel={() => setPicking(null)}
          returnFocusTo={picking.trigger}
        />
      )}
    </>
  );
}

/** One folder under a group: its visibility switch (on = shown), its name, and — only when no
 *  project owns it — Rename. */
function FolderRow({
  cwd,
  depth,
  stale,
  visible,
  adopted,
  name,
  onToggle,
  onRename,
}: {
  cwd: string;
  depth: number;
  stale: boolean;
  visible: boolean;
  adopted: boolean;
  name: string;
  onToggle: (cwd: string, visible: boolean) => void;
  onRename: (cwd: string, trigger: HTMLElement) => void;
}) {
  const custom = name.trim();
  return (
    <li className={visible ? undefined : styles.isOff} style={depth ? { paddingInlineStart: `calc(var(--sp-3) + ${depth} * var(--sp-4))` } : undefined}>
      <Switch
        checked={visible}
        onChange={(next) => onToggle(cwd, next)}
        label={
          adopted
            ? `Offer ${shortCwd(cwd)} as a launch location`
            : `Show ${shortCwd(cwd)} in the sidebar, filter, and overview`
        }
      />
      <span className={styles.fname}>
        {/* An adopted folder is grouped under — and labelled by — its PROJECT everywhere the app
            names a group, so a custom name would be stored and shown nowhere (#615 Phase 3). */}
        <span title={adopted ? "Named by its project — rename the project instead" : cwd}>
          {adopted ? shortCwd(cwd) : custom || shortCwd(cwd)}
        </span>
        <span>{adopted ? "Named by its project" : custom ? shortCwd(cwd) : "no custom name"}</span>
      </span>
      {stale ? <span className="nj-stamp">Not currently active</span> : <span />}
      {adopted ? (
        <span />
      ) : (
        <button
          type="button"
          className={`${action.ghost} ${action.compact}`}
          onClick={(e) => onRename(cwd, e.currentTarget)}
          aria-label={`Rename ${shortCwd(cwd)}`}
        >
          Rename
        </button>
      )}
    </li>
  );
}

function OverviewVisibility() {
  const {
    hiddenProjects,
    includedProjects,
    projectsMode,
    isVisible,
    setProjectVisible,
    setProjectsMode,
    projectNames,
    setProjectName,
  } = useOverviewPrefs();
  const discovered = useDiscoveredFolders(useDiscoveryKey());
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  const [renaming, setRenaming] = useState<{ cwd: string; trigger: HTMLElement | null } | null>(null);

  useEffect(() => {
    let alive = true;
    // Entities drive the grouping (#465). Mount-only: ownership doesn't depend on the scope. A
    // failed read leaves every folder under Unassigned, as before.
    api
      .projectEntities()
      .then((d) => alive && setEntities(d.projects))
      .catch(() => alive && setEntities([]));
    return () => {
      alive = false;
    };
  }, []);

  // Group the discovered (∪ curated-but-inactive ∪ named) folders by owning entity (#465), each
  // group a nesting tree. A curated-but-inactive folder or a rename for an inactive one stays
  // editable here.
  const groups = useMemo(() => {
    const known = new Set((discovered.data ?? []).map((p) => p.cwd));
    const all = new Set<string>([...known, ...hiddenProjects, ...includedProjects, ...Object.keys(projectNames)]);
    const byOwner = new Map<string, Set<string>>();
    for (const cwd of all) {
      const owner = owningProjectId(cwd, entities);
      const bucket = byOwner.get(owner) ?? new Set<string>();
      bucket.add(cwd);
      byOwner.set(owner, bucket);
    }
    const toRows = (cwds: Set<string>) =>
      flattenTree(buildProjectTree(cwds)).map((n) => ({ cwd: n.cwd, depth: n.depth, stale: !known.has(n.cwd) }));
    const owned = entities
      .filter((e) => (byOwner.get(e.id)?.size ?? 0) > 0)
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((e) => ({ entity: e, rows: toRows(byOwner.get(e.id)!) }));
    const loose = byOwner.get("");
    return { owned, unassigned: loose && loose.size > 0 ? toRows(loose) : [] };
  }, [discovered.data, entities, hiddenProjects, includedProjects, projectNames]);

  const curated = projectsMode === "included";
  const total = groups.owned.reduce((n, g) => n + g.rows.length, 0) + groups.unassigned.length;
  const row = (r: { cwd: string; depth: number; stale: boolean }, adopted: boolean) => (
    <FolderRow
      key={r.cwd}
      cwd={r.cwd}
      depth={r.depth}
      stale={r.stale}
      visible={isVisible(r.cwd)}
      adopted={adopted}
      name={projectNames[r.cwd] ?? ""}
      onToggle={(cwd, visible) => setProjectVisible(cwd, visible)}
      onRename={(cwd, trigger) => setRenaming({ cwd, trigger })}
    />
  );
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="overview-h">
      <Callsign as="h2" id="overview-h" title="Overview visibility" value={curated ? "only included" : "show all"} />
      {/* Visibility mode (#335). "Show all" = the denylist (switch off to hide). "Only included" =
          a curated allowlist: only folders switched on show, and a new directory never appears
          until you switch it on (starting a session in a directory also adds it). */}
      <Seg
        className={styles.seg}
        label="Project visibility"
        value={curated ? "included" : "all"}
        onChange={(m) => setProjectsMode(m)}
        options={[
          { value: "all", label: "Show all (hide a few)" },
          { value: "included", label: "Only included" },
        ]}
      />
      {/* #615: what switching off does, per row kind. */}
      <p className={styles.text}>
        {curated
          ? "Only folders switched on are offered as launch locations; a folder switched off that no project has adopted also leaves the sidebar, the filter and the overview map. New directories stay off until you switch them on (starting a session in one adds it)."
          : "Switching a folder off stops it being offered as a launch location. If no project has adopted it, its sessions also leave the sidebar, the filter and the overview map."}{" "}
        A project’s own sessions stay visible — archive the project to hide those.
      </p>
      {/* A failed read is said whatever was read before: red when there is nothing to show, amber
          with the last list (from the previous scope) when there is. Never the empty line. */}
      {discovered.error && (discovered.data === null || total === 0) ? (
        <ReadFailed
          title="Couldn’t read the discovered folders"
          detail={`${discovered.error}. Project roots and exclusions still save; the folder list returns when discovery answers.`}
          onRetry={discovered.retry}
          testId="discovery-read-failed"
        />
      ) : discovered.data === null ? (
        <p className={styles.quiet}>Loading folders…</p>
      ) : total === 0 ? (
        <p className={styles.quiet}>No folders discovered yet.</p>
      ) : (
        <>
          {discovered.error && (
            <StaleNotice onRetry={discovered.retry} testId="discovery-stale">
              Couldn’t refresh the discovered folders — {discovered.error}. The list below is the last
              one read and may not match the current roots and exclusions.
            </StaleNotice>
          )}
          {groups.owned.length > 0 && (
            <>
              <p className={styles.groupHead}>Adopted by a project · managed on its page</p>
              {groups.owned.map(({ entity, rows }) => (
                <div key={entity.id} className={styles.group}>
                  <div className={styles.groupTitle}>
                    <ProjectSwatch color={entity.color} />
                    <b>{entity.name}</b>
                    <span className={styles.sub}>
                      {rows.length} folder{rows.length === 1 ? "" : "s"}
                    </span>
                    <Link to={projectPath(entity.id)} className={styles.link} aria-label={`Open ${entity.name}`}>
                      Open <ArrowRight size={13} aria-hidden="true" />
                    </Link>
                  </div>
                  <ul className={styles.folders} aria-label={`Folders in ${entity.name}`}>
                    {rows.map((r) => row(r, true))}
                  </ul>
                </div>
              ))}
            </>
          )}
          {groups.unassigned.length > 0 && (
            <>
              <p className={styles.groupHead}>
                Unassigned · {groups.unassigned.length} folder{groups.unassigned.length === 1 ? "" : "s"}
              </p>
              <ul className={`${styles.folders} ${styles.unassigned}`} aria-label="Folders in Unassigned">
                {groups.unassigned.map((r) => row(r, false))}
              </ul>
            </>
          )}
        </>
      )}
      {renaming && (
        <RenameProjectModal
          cwd={renaming.cwd}
          initialName={projectNames[renaming.cwd] ?? ""}
          onCancel={() => setRenaming(null)}
          onSave={(name) => {
            setProjectName(renaming.cwd, name);
            setRenaming(null);
          }}
          returnFocusTo={renaming.trigger}
        />
      )}
    </Panel>
  );
}
