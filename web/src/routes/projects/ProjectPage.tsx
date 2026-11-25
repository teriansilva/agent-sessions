/** One project's page (#2143): everything its setup needs, in tabs — General · Session defaults ·
 *  Playbook (#1949) · Memory & equipment. The page reads the project list
 *  (archived included) and finds its project there: the same read and the same PATCH / archive /
 *  delete calls Settings → Projects made, from a new route. */
import { ArrowLeft, Folder, Star } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useConfig } from "../../app/config";
import { Tabs } from "../../components/hud/Controls";
import { Panel } from "../../components/hud/Panel";
import { PROJECT_TABS, PROJECTS_PATH, type ProjectTab } from "../../lib/routes";
import { ProjectDefaults } from "./ProjectDefaults";
import { ProjectGeneral } from "./ProjectGeneral";
import { ProjectMemory } from "./ProjectMemory";
import { ProjectPlaybookTab } from "./ProjectPlaybookTab";
import { ProjectSwatch, ReadFailed, StaleNotice, TabLabel } from "./ProjectBits";
import { useProjectEntities } from "./projectsData";
import styles from "./projects.module.css";

const TAB_LABELS: Record<ProjectTab, { long: string; short: string }> = {
  general: { long: "General", short: "General" },
  defaults: { long: "Session defaults", short: "Defaults" },
  playbook: { long: "Playbook", short: "Playbook" },
  memory: { long: "Memory & equipment", short: "Memory" },
};

function asTab(v: string | null): ProjectTab {
  return (PROJECT_TABS as readonly string[]).includes(v ?? "") ? (v as ProjectTab) : "general";
}

export default function ProjectPage() {
  const { pid = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = asTab(params.get("tab"));
  const entities = useProjectEntities();
  const config = useConfig();
  const entity = entities.data?.find((e) => e.id === pid) ?? null;
  const starred = !!entity && config?.default_project_id === entity.id;
  const choose = (next: ProjectTab) => setParams(next === "general" ? {} : { tab: next }, { replace: true });

  const back = (
    <Link to={PROJECTS_PATH} className={styles.back}>
      <ArrowLeft size={14} aria-hidden="true" /> All projects
    </Link>
  );
  if (!entity) {
    return (
      <div className={styles.page} data-testid="project-page">
        {back}
        {entities.data === null && entities.error ? (
          <ReadFailed
            title="Couldn’t load the project"
            detail={`${entities.error}. Nothing was changed.`}
            onRetry={entities.retry}
            testId="project-read-failed"
          />
        ) : entities.data === null ? (
          <p className={styles.quiet}>Loading the project…</p>
        ) : (
          <Panel tone="quiet" rail="none" className={styles.empty} data-testid="project-missing">
            <p className={styles.emptyTitle}>No such project</p>
            <p className={styles.quiet}>
              It may have been deleted. Every project is listed on the Projects page.
            </p>
          </Panel>
        )}
      </div>
    );
  }
  const folders = `${entity.folders.length} folder${entity.folders.length === 1 ? "" : "s"}`;
  return (
    <div className={styles.page} data-testid="project-page">
      {back}
      <header className={styles.phead}>
        <div className={styles.ptitle}>
          <ProjectSwatch color={entity.color} large />
          <div className={styles.headText}>
            <div className={`nj-kicker ${styles.kicker}`}>
              <span className="nj-notch" aria-hidden="true" />
              HQ // Projects // <b>{entity.name}</b>
            </div>
            <h1 id="project-h" tabIndex={-1}>
              {entity.name}
            </h1>
          </div>
        </div>
      </header>
      <div className={styles.chips}>
        {starred && (
          <span className="nj-chip is-on">
            <Star size={13} fill="currentColor" aria-hidden="true" /> Default
          </span>
        )}
        {entity.archived && <span className="nj-chip">Archived</span>}
        <span className="nj-chip">
          <Folder size={13} aria-hidden="true" /> {folders}
        </span>
        <span className="nj-chip">
          {entity.session_count} session{entity.session_count === 1 ? "" : "s"}
        </span>
      </div>
      {entities.error && (
        <StaleNotice onRetry={entities.retry}>
          Couldn’t refresh the project — {entities.error}. Showing the last one read.
        </StaleNotice>
      )}
      <Tabs
        className={styles.tabs}
        label={`${entity.name} settings`}
        value={tab}
        onChange={choose}
        tabs={PROJECT_TABS.map((id) => ({ id, label: <TabLabel {...TAB_LABELS[id]} /> }))}
        panel={(id) =>
          id === "defaults" ? (
            <ProjectDefaults key={entity.id} entity={entity} onSaved={entities.reload} />
          ) : id === "playbook" ? (
            <ProjectPlaybookTab key={entity.id} entity={entity} />
          ) : id === "memory" ? (
            <ProjectMemory key={entity.id} entity={entity} />
          ) : (
            <ProjectGeneral key={entity.id} entity={entity} starred={starred} onChanged={entities.reload} />
          )
        }
      />
    </div>
  );
}
