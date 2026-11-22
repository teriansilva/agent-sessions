/** HQ → Projects (#2143): every project, and the folders discovery offers them. Settings → Projects
 *  moved here: the project list (one row = one link to the project's own page, where everything
 *  else lives), the Archived disclosure with Unarchive, and — on the second tab — folder discovery
 *  and overview visibility, unchanged in behaviour.
 *
 *  Each column's source is its own read and fails on its own: the list failing is the page's red
 *  state (never "No projects yet"); a playbook or memory column failing says so in its cells. */
import { ArchiveRestore, ChevronRight, Plus, Star } from "lucide-react";
import { useCallback, useState, type CSSProperties } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useConfig } from "../../app/config";
import { engineColor, engineLabel } from "../../app/engineRoster";
import { Disclosure, Tabs } from "../../components/hud/Controls";
import { Panel } from "../../components/hud/Panel";
import action from "../../components/ui/actionButton.module.css";
import { api, ApiError } from "../../lib/api";
import { shortCwd } from "../../lib/format";
import { describeLaunchDefaults } from "../../lib/launchDefaults";
import type { WizardEntryState } from "../../lib/newProject";
import { NEW_PROJECT_PATH, projectPath, type ProjectsIndexTab } from "../../lib/routes";
import { useProjectMemory } from "../../lib/projectMemoryStore";
import type { ProjectArchiveReport, ProjectEntity } from "../../types/api";
import type { MemoryProject } from "../../types/memory";
import { FoldersDiscovery } from "./FoldersDiscovery";
import { BulkReport, ProjectSwatch, ReadFailed, StaleNotice, TabLabel } from "./ProjectBits";
import {
  splitProjects,
  useMemoryRows,
  usePlaybookFleet,
  useProjectEntities,
  type ProjectPlaybookInfo,
  type Read,
} from "./projectsData";
import styles from "./projects.module.css";

export default function ProjectsIndex() {
  const [params, setParams] = useSearchParams();
  const tab: ProjectsIndexTab = params.get("tab") === "folders" ? "folders" : "projects";
  const entities = useProjectEntities();
  const count = entities.data ? splitProjects(entities.data).active.length : undefined;
  const choose = (next: ProjectsIndexTab) =>
    setParams(next === "folders" ? { tab: "folders" } : {}, { replace: true });
  return (
    <div className={styles.page} data-testid="projects-index">
      <header className={styles.head}>
        <div className={styles.headText}>
          <div className={`nj-kicker ${styles.kicker}`}>
            <span className="nj-notch" aria-hidden="true" />
            HQ // <b>Projects</b>
          </div>
          {/* tabIndex -1: where focus lands when a row a dialog was opened from is gone. */}
          <h1 id="projects-h" tabIndex={-1}>
            Projects
          </h1>
          <p className={styles.lede}>
            Every project’s setup in one place: folders, session defaults, playbook and memory.
            Projects are metadata — assigning or archiving never moves session files.
          </p>
        </div>
        {/* Creating a project is the New project wizard's job (#1187); Done returns here. */}
        <Link
          to={NEW_PROJECT_PATH}
          state={{ from: "projects" } satisfies WizardEntryState}
          className={action.primary}
        >
          <Plus size={14} aria-hidden="true" /> New project
        </Link>
      </header>
      <Tabs
        className={styles.tabs}
        label="Projects views"
        value={tab}
        onChange={choose}
        tabs={[
          { id: "projects", label: <TabLabel long="Projects" count={count} /> },
          { id: "folders", label: <TabLabel long="Folders & discovery" short="Folders" /> },
        ]}
        panel={(id) => (id === "folders" ? <FoldersDiscovery /> : <ProjectList entities={entities} />)}
      />
    </div>
  );
}

function ProjectList({ entities }: { entities: Read<ProjectEntity[]> & { reload: () => Promise<unknown> } }) {
  const config = useConfig();
  const memory = useMemoryRows();
  const fleet = usePlaybookFleet();
  const [report, setReport] = useState<ProjectArchiveReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const starred = config?.default_project_id ?? "";
  const global = {
    engine: config?.agent_defaults?.default_engine ?? null,
    bypass: config?.agent_defaults?.bypass ?? true,
  };

  const unarchive = useCallback(
    async (id: string) => {
      setError(null);
      try {
        setReport(await api.unarchiveProject(id));
        await entities.reload();
      } catch (e) {
        setError(e instanceof ApiError && e.message ? e.message : "Couldn’t update the project.");
      }
    },
    [entities],
  );

  if (entities.data === null) {
    if (entities.error)
      return (
        <ReadFailed
          title="Couldn’t load projects"
          detail={`${entities.error}. Nothing was changed.`}
          onRetry={entities.retry}
          testId="projects-read-failed"
        />
      );
    return <p className={styles.quiet}>Loading projects…</p>;
  }
  const { active, archived } = splitProjects(entities.data);
  return (
    <div className={styles.body}>
      {entities.error && (
        <StaleNotice onRetry={entities.retry}>
          Couldn’t refresh the project list — {entities.error}. Showing the last one read.
        </StaleNotice>
      )}
      {error && (
        <p className="nj-error" role="alert">
          {error}
        </p>
      )}
      {report && (
        <BulkReport
          report={report}
          onRetry={() => void unarchive(report.id)}
          onDismiss={() => setReport(null)}
        />
      )}
      {active.length === 0 ? (
        <Panel tone="quiet" rail="none" className={styles.empty} data-testid="projects-empty">
          <p className={styles.emptyTitle}>No projects yet</p>
          <p className={styles.quiet}>
            Start one with New project: a name, a folder (new or existing), a colour and, if you
            like, a playbook.
          </p>
          <Link to={NEW_PROJECT_PATH} state={{ from: "projects" } satisfies WizardEntryState} className={action.primary}>
            <Plus size={14} aria-hidden="true" /> New project
          </Link>
        </Panel>
      ) : (
        <>
          <div className={styles.lhead} aria-hidden="true">
            <span>Project</span>
            <span>Session defaults</span>
            <span>Playbook</span>
            <span>Memory</span>
            <span className={styles.num}>Sessions</span>
            <span />
          </div>
          <ul className={styles.list} aria-label="Projects">
            {active.map((e) => (
              <ProjectRow
                key={e.id}
                entity={e}
                starred={e.id === starred}
                global={global}
                memory={memory}
                fleet={fleet}
              />
            ))}
          </ul>
        </>
      )}
      {archived.length > 0 && (
        <Disclosure
          className={styles.archived}
          testId="projects-archived"
          summary={<span>Archived ({archived.length})</span>}
        >
          <ul className={styles.paths} aria-label="Archived projects">
            {archived.map((e) => (
              <li key={e.id}>
                <ProjectSwatch color={e.color} />
                <Link to={projectPath(e.id)} className={styles.pathLink}>
                  {e.name}
                </Link>
                <button
                  type="button"
                  className={`${action.ghost} ${action.compact}`}
                  aria-label={`Unarchive project ${e.name}`}
                  onClick={() => void unarchive(e.id)}
                >
                  <ArchiveRestore size={13} aria-hidden="true" /> Unarchive
                </button>
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </div>
  );
}

function ProjectRow({
  entity: e,
  starred,
  global,
  memory,
  fleet,
}: {
  entity: ProjectEntity;
  starred: boolean;
  global: { engine: string | null; bypass: boolean };
  memory: Read<MemoryProject[]>;
  fleet: Read<Record<string, ProjectPlaybookInfo>>;
}) {
  const defaults = e.launch_defaults ?? {};
  const folders = `${e.folders.length} folder${e.folders.length === 1 ? "" : "s"}`;
  const engine = defaults.engine;
  return (
    <li>
      <Link to={projectPath(e.id)} className={`nj-plate ${styles.row}`} data-testid="project-row">
        <span className={styles.who}>
          <ProjectSwatch color={e.color} />
          <span className={styles.name}>
            <span className={styles.nameText}>{e.name}</span>
            {starred && (
              <>
                <Star size={14} className={styles.star} fill="currentColor" aria-hidden="true" />
                <span className="nj-sr-only">(default project)</span>
              </>
            )}
          </span>
          <span className={styles.sub}>
            <b>{folders}</b>
            {" · "}
            {e.default_folder ? (
              <span title={e.default_folder}>{shortCwd(e.default_folder)}</span>
            ) : (
              <span className={styles.warn}>no default folder</span>
            )}
          </span>
        </span>
        <span className={`${styles.cell} ${styles.isDefaults}`}>
          <span className={styles.cellLabel}>Defaults</span>
          <span className={styles.line}>
            <span
              className={styles.eng}
              style={engine ? ({ "--eng": engineColor(engine) } as CSSProperties) : undefined}
              aria-hidden="true"
            />
            {engine ? engineLabel(engine) : "Global default"}
          </span>
          <span className={styles.quietLine} data-testid="project-launch-defaults">
            {describeLaunchDefaults(defaults, global)}
          </span>
        </span>
        <span className={`${styles.cell} ${styles.isPlaybook}`}>
          <span className={styles.cellLabel}>Playbook</span>
          <PlaybookCell pid={e.id} fleet={fleet} />
        </span>
        <span className={`${styles.cell} ${styles.isMemory}`}>
          <span className={styles.cellLabel}>Memory</span>
          <MemoryCell pid={e.id} memory={memory} />
        </span>
        <span className={`${styles.n} ${styles.num} ${styles.isSessions}`}>
          {e.session_count}
          <span className="nj-sr-only"> session{e.session_count === 1 ? "" : "s"}</span>
        </span>
        <ChevronRight size={16} className={styles.chev} aria-hidden="true" />
      </Link>
    </li>
  );
}

function PlaybookCell({ pid, fleet }: { pid: string; fleet: Read<Record<string, ProjectPlaybookInfo>> }) {
  if (fleet.data === null)
    return <span className={styles.quietLine}>{fleet.error ? "Unavailable" : "…"}</span>;
  const info = fleet.data[pid];
  if (!info) return <span className={styles.quietLine}>No playbook</span>;
  // The fleet read reports the deployment's state (apply.status): only an APPLIED deployment is the
  // project's equipment. A bound-only or interrupted one is never drawn as current, whatever its
  // revision. The index never verifies.
  const applied = info.state === "applied";
  const stateLine =
    info.state === "bound"
      ? "Bound — not applied yet"
      : info.state === "interrupted"
        ? "Interrupted — needs recovery"
        : `State: ${info.state}`;
  return (
    <>
      <span className={styles.line}>
        {info.name}
        {applied && !info.updateAvailable && info.version && <span className="nj-stamp">v{info.version}</span>}
      </span>
      {!applied ? (
        <span className={`${styles.quietLine} ${styles.warn}`} data-testid="project-playbook-state">
          {stateLine}
        </span>
      ) : info.updateAvailable ? (
        <span className={styles.line}>
          <span className={`nj-stamp ${styles.stampAccent}`}>
            Update{info.version ? ` → v${info.version}` : " available"}
          </span>
        </span>
      ) : (
        <span className={styles.quietLine}>Current revision</span>
      )}
    </>
  );
}

function MemoryCell({ pid, memory }: { pid: string; memory: Read<MemoryProject[]> }) {
  const row = memory.data?.find((m) => m.id === pid);
  const shared = useProjectMemory(pid);
  if (!row) return <span className={styles.quietLine}>{memory.error ? "Unavailable" : memory.data ? "—" : "…"}</span>;
  const on = shared ?? row.enabled;
  return (
    <>
      <span className={styles.onoff} data-on={on}>
        {on ? "On" : "Off"}
      </span>
      <span className={styles.quietLine}>{on ? `${row.notes} note${row.notes === 1 ? "" : "s"}` : "—"}</span>
    </>
  );
}
