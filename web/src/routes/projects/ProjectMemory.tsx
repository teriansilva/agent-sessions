/** Project → Memory & equipment (#2143): the project's memory switch (the same switch and endpoint
 *  as Library → Memory, through ONE shared store — `projectMemoryStore`), and read-only lists of
 *  what the project is equipped with: the secrets bound to it and the automations that target it.
 *  Those are edited where they live; each list links out. Every list reads on its own and fails in
 *  place while the rest keeps working. */
import { ArrowRight, Braces, KeyRound } from "lucide-react";
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Switch } from "../../components/hud/Controls";
import { Callsign, Panel } from "../../components/hud/Panel";
import { Led } from "../../components/hud/Meters";
import { StateWord } from "../../components/automations/AutomationBits";
import { api, ApiError } from "../../lib/api";
import { setProjectMemory, useProjectMemory } from "../../lib/projectMemoryStore";
import { AUTOMATIONS_PATH, automationPath, MEMORY_PATH, VARIABLES_PATH } from "../../lib/routes";
import type { ProjectEntity } from "../../types/api";
import type { Automation } from "../../types/automations";
import type { SharedVariable } from "../../types/variables";
import { ReadFailed } from "./ProjectBits";
import { useMemoryRows, useRead } from "./projectsData";
import styles from "./projects.module.css";

/** An automation targets a project when it starts a mission in it, or a session in one of its
 *  folders. */
function targets(a: Automation, entity: ProjectEntity): boolean {
  const act = a.action;
  if (!act) return false;
  if (act.kind === "start_mission") return act.project_id === entity.id;
  if (act.kind === "start_session") return entity.folders.includes(act.folder);
  return false;
}

function actionLine(a: Automation): string {
  const kind = a.action?.kind === "start_mission" ? "Starts a mission" : a.action?.kind === "start_session" ? "Starts a session" : "Sends a message";
  return a.paused ? `${kind} · paused` : kind;
}

export function ProjectMemory({ entity }: { entity: ProjectEntity }) {
  return (
    <div className={styles.body}>
      <MemoryPanel entity={entity} />
      <div className={styles.cols}>
        <VariablesPanel entity={entity} />
        <AutomationsPanel entity={entity} />
      </div>
    </div>
  );
}

function MemoryPanel({ entity }: { entity: ProjectEntity }) {
  const rows = useMemoryRows();
  const row = rows.data?.find((m) => m.id === entity.id) ?? null;
  const shared = useProjectMemory(entity.id);
  const enabled = shared ?? row?.enabled ?? false;
  const [busy, setBusy] = useState(false);
  // A message belongs to the project it is about (the page is keyed by project, so a late answer
  // for another project is never shown here).
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const toggle = async (next: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      const now = await setProjectMemory(entity.id, next);
      setNote({
        text: now
          ? "Memory is on. Sessions already running join after a restart."
          : "Memory is off. New sessions won't get it; running ones keep their paths.",
        error: false,
      });
      rows.retry();
    } catch (e) {
      setNote({
        text: `${e instanceof ApiError && e.message ? e.message : "Couldn’t change memory."} The switch shows what is stored.`,
        error: true,
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="memory-h">
      <Callsign as="h2" id="memory-h" title="Project memory" value={row || shared !== undefined ? (enabled ? "on" : "off") : undefined} />
      {rows.data === null && rows.error && shared === undefined ? (
        <ReadFailed
          title="Couldn’t read project memory"
          detail={`${rows.error}. Nothing was changed.`}
          onRetry={rows.retry}
          testId="memory-read-failed"
        />
      ) : rows.data === null && shared === undefined ? (
        <p className={styles.quiet}>Reading memory…</p>
      ) : (
        <>
          <div className={styles.setRow}>
            <div>
              <p className={styles.setTitle}>Shared memory for {entity.name}</p>
              <p className={styles.text}>
                Every new session in this project gets the BattleLab memory tool: a notebook the
                project’s agents read and write. Sessions already running join after a restart.
              </p>
            </div>
            <Switch
              checked={enabled}
              onChange={(next) => void toggle(next)}
              label={`Shared memory for ${entity.name}`}
              disabled={busy}
              testId="project-memory-switch"
            />
          </div>
          {row && (
            <dl className={styles.readouts}>
              <div>
                <dt>Notes</dt>
                <dd className="nj-readout">{row.notes}</dd>
              </div>
              <div>
                <dt>Inbox</dt>
                <dd className="nj-readout">
                  {row.pending}
                  <span className="nj-unit">pending</span>
                </dd>
              </div>
            </dl>
          )}
          {note && (
            <p className={note.error ? "nj-error" : styles.quiet} role={note.error ? "alert" : "status"} data-testid="project-memory-note">
              {note.text}
            </p>
          )}
          <Link to={`${MEMORY_PATH}?project=${encodeURIComponent(entity.id)}`} className={styles.link}>
            Open notebook in Library → Memory <ArrowRight size={13} aria-hidden="true" />
          </Link>
        </>
      )}
    </Panel>
  );
}

function VariablesPanel({ entity }: { entity: ProjectEntity }) {
  const read = useRead<SharedVariable[]>(
    useCallback(() => api.variables().then((d) => d.variables.filter((v) => v.projects?.includes(entity.id))), [entity.id]),
    "The shared library didn’t answer.",
  );
  const bound = read.data ?? [];
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="variables-h">
      <Callsign as="h2" id="variables-h" title="Variables and secrets" value={read.data ? `${bound.length} bound` : undefined} />
      <p className={styles.text}>Read only here. What this project’s agents can use.</p>
      {read.data === null && read.error ? (
        <ReadFailed
          title="Couldn’t load variables"
          detail={`${read.error}. Bindings are unchanged.`}
          onRetry={read.retry}
          testId="variables-read-failed"
        />
      ) : read.data === null ? (
        <p className={styles.quiet}>Loading…</p>
      ) : bound.length === 0 ? (
        <p className={styles.quiet}>No secrets are bound to this project.</p>
      ) : (
        <ul className={styles.rows} aria-label={`Variables bound to ${entity.name}`}>
          {bound.map((v) => (
            <li key={v.id ?? v.name}>
              <span className={styles.rowMain}>
                <b>
                  {v.kind === "text" ? <Braces size={13} aria-hidden="true" /> : <KeyRound size={13} aria-hidden="true" />}
                  {v.name}
                  <span className="nj-stamp">{v.kind === "secret_file" ? "file" : v.kind}</span>
                </b>
                <span>{v.agent_access ? "Agents may request a private copy" : "Bound by reference"}</span>
              </span>
              <span className={styles.ledWord}>
                <Led tone={v.set ? "up" : "att"} /> {v.set ? "Set" : "Not set"}
              </span>
            </li>
          ))}
        </ul>
      )}
      <Link to={VARIABLES_PATH} className={styles.link}>
        Manage in Library → Variables <ArrowRight size={13} aria-hidden="true" />
      </Link>
    </Panel>
  );
}

function AutomationsPanel({ entity }: { entity: ProjectEntity }) {
  const read = useRead<Automation[]>(
    useCallback(() => api.automations().then((d) => d.automations.filter((a) => targets(a, entity))), [entity]),
    "The automations store didn’t answer.",
  );
  const list = read.data ?? [];
  return (
    <Panel tone="quiet" className={styles.panel} aria-labelledby="automations-h">
      <Callsign
        as="h2"
        id="automations-h"
        title="Automations"
        value={read.data ? `${list.length} target${list.length === 1 ? "s" : ""} this project` : undefined}
      />
      <p className={styles.text}>Read only here. Edit, run or pause them in Automations.</p>
      {read.data === null && read.error ? (
        <ReadFailed
          title="Couldn’t load automations"
          detail={`${read.error}. Nothing was paused or changed.`}
          onRetry={read.retry}
          testId="automations-read-failed"
        />
      ) : read.data === null ? (
        <p className={styles.quiet}>Loading…</p>
      ) : list.length === 0 ? (
        <p className={styles.quiet}>No automation targets this project.</p>
      ) : (
        <ul className={styles.rows} aria-label={`Automations targeting ${entity.name}`}>
          {list.map((a) => (
            <li key={a.id}>
              <span className={styles.rowMain}>
                <b className={styles.isSans}>{a.name}</b>
                <span>{actionLine(a)}</span>
              </span>
              <span className={styles.ledWord}>
                <StateWord state={a.state} />
              </span>
              <Link to={automationPath(a.id)} className={styles.link} aria-label={`Open automation ${a.name}`}>
                Open <ArrowRight size={13} aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
      )}
      <Link to={AUTOMATIONS_PATH} className={styles.link}>
        Manage in Automations <ArrowRight size={13} aria-hidden="true" />
      </Link>
    </Panel>
  );
}
