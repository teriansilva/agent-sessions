import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { engineInfo, engineLabel } from "../../app/engineRoster";
import { shortCwd } from "../../lib/format";
import type { ProjectPickerEntity } from "../../lib/projectPickerStore";
import type { UnavailableClient } from "../../types/api";
import styles from "./NewSessionControls.module.css";

/** The New session form's three pickers (#1436). Each is a group of NATIVE radios drawn as tiles or
 *  rows, so arrow keys, Tab order and screen-reader semantics are the browser's own — the visible
 *  box is only the radio's label. Which agents and projects exist is the caller's business: these
 *  never name one. */

/** What an agent tile says under its name: an API client names the CLI it drives (what the old
 *  "API client · ready" note explained in a paragraph); a chat agent has no tools. */
function agentSubtitle(id: string): string {
  const info = engineInfo(id);
  if (info?.runtime === "chat") return "chat · no tools";
  const source = info?.api?.source;
  return source ? `via ${engineLabel(source)} CLI` : "";
}

export function AgentTiles({
  consoleEngines,
  apiEngines,
  unavailable,
  value,
  defaultEngine,
  projectEngine = "",
  onChange,
}: {
  consoleEngines: string[];
  apiEngines: string[];
  unavailable: UnavailableClient[];
  value: string;
  defaultEngine: string;
  /** The selected project's own agent (#1508), badged `project` — it outranks the global default. */
  projectEngine?: string;
  onChange: (id: string) => void;
}) {
  const tile = (id: string) => {
    const sub = agentSubtitle(id);
    return (
      <label key={id} className={styles.tile} data-engine={id}>
        <input
          type="radio"
          name="new-session-agent"
          value={id}
          checked={value === id}
          onChange={() => onChange(id)}
        />
        <span className={styles.tileName}>
          {engineLabel(id)}
          {id === projectEngine ? (
            <span className={styles.badge}>project</span>
          ) : (
            id === defaultEngine && !projectEngine && <span className={styles.badge}>default</span>
          )}
        </span>
        {sub && <span className={styles.tileSub}>{sub}</span>}
      </label>
    );
  };
  return (
    <div role="radiogroup" aria-label="Agent" className={styles.group}>
      {consoleEngines.length > 0 && (
        <>
          <span className={styles.groupHead}>Terminal</span>
          <div className={styles.tiles}>{consoleEngines.map(tile)}</div>
        </>
      )}
      {(apiEngines.length > 0 || unavailable.length > 0) && (
        <>
          <span className={styles.groupHead}>API — structured, no terminal</span>
          <div className={styles.tiles}>
            {apiEngines.map(tile)}
            {/* A client that cannot start is listed, disabled, with its reason ON the tile — its
                absence is explained, never silent (#1311), without a separate box. */}
            {unavailable.map((c) => (
              <label
                key={`unavailable:${c.id}`}
                className={styles.tile}
                data-testid="new-session-unavailable"
              >
                <input type="radio" name="new-session-agent" value={`unavailable:${c.id}`} disabled />
                <span className={styles.tileName}>
                  <span className={styles.ledDown} aria-hidden="true" />
                  {c.label}
                </span>
                <span className={styles.tileSub}>
                  <span className="sr-only">unavailable: </span>
                  {c.reason}
                </span>
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

const NON_TEXT_INPUTS = new Set(["radio", "checkbox", "button", "submit", "reset", "range", "color", "file", "image"]);
/** A control that takes typed characters — where `/` is a character, not a shortcut. */
function isTextEntry(el: HTMLElement): boolean {
  if (el instanceof HTMLInputElement) return !NON_TEXT_INPUTS.has(el.type);
  return el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
}

/** Above this many projects the list gets a filter field (`/` focuses it). */
const FILTER_FROM = 7;

export function ProjectList({
  projects,
  value,
  starredId,
  loading,
  failed,
  newProjectTo,
  newProjectState,
  onNewProject,
  onChange,
}: {
  /** `null` = never loaded: skeleton rows of the final height, so nothing jumps when it lands. */
  projects: ProjectPickerEntity[] | null;
  /** The checked row; `null` = none yet (the default is not known until the list lands). */
  value: string | null;
  starredId: string;
  loading: boolean;
  failed: boolean;
  newProjectTo: string;
  newProjectState: unknown;
  onNewProject: (e: MouseEvent<HTMLAnchorElement>) => void;
  onChange: (id: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const filterRef = useRef<HTMLInputElement>(null);
  const showFilter = (projects?.length ?? 0) >= FILTER_FROM;
  useEffect(() => {
    if (!showFilter) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      // From inside the New session form, or with nothing focused (the body) — never from the
      // sidebar or a dialog (the folder picker renders outside the form) (#1437 review)…
      // Never while a modal is open — whatever has focus: a click on a dialog's padding drops it to
      // the body, and while the folder picker loads it is still on a control in this form
      // (#1437 reviews).
      if (document.querySelector('[aria-modal="true"]')) return;
      if (!t || (t !== document.body && !t.closest?.("[data-new-session-form]"))) return;
      // …and never while typing. A radio or a checkbox is not typing: focus sits on one after a
      // tile or row is clicked, and `/` must still reach the filter from there.
      if (t.isContentEditable || isTextEntry(t)) return;
      e.preventDefault();
      filterRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showFilter]);

  const q = filter.trim().toLowerCase();
  const shown = (projects ?? []).filter(
    (p) =>
      !q ||
      p.id === value ||
      p.name.toLowerCase().includes(q) ||
      p.default_folder.toLowerCase().includes(q),
  );
  const row = (id: string, body: ReactNode, extra?: { color?: string }) => (
    <label key={id || "none"} className={styles.row}>
      <input
        type="radio"
        name="new-session-project"
        value={id}
        checked={value === id}
        onChange={() => onChange(id)}
      />
      <span
        className={styles.swatch}
        style={extra?.color ? { background: extra.color } : undefined}
        aria-hidden="true"
      />
      {body}
    </label>
  );

  return (
    <div className={styles.group}>
      {/* ONE fixed-height box at every list size, the filter inside it (#1437): the skeleton, a
          two-project list and a forty-project list all take the same room, so the folder card
          below never moves when the list lands. */}
      <div className={styles.list}>
        {showFilter && (
          <input
            ref={filterRef}
            type="search"
            className={styles.filter}
            aria-label="Filter projects"
            placeholder="Filter projects…  /"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        )}
        <div role="radiogroup" aria-label="Project" aria-busy={loading} className={styles.rows}>
        {/* "No project" is a real row even while the list loads (#1437 review): it needs no list
            to confirm, so it is the way out of a request that never answers. */}
        {row("", <span className={styles.rowNone}>No project</span>)}
        {projects === null && !failed ? (
          [0, 1].map((i) => (
            <div key={i} className={styles.skeleton} data-testid="project-skeleton" aria-hidden="true">
              <span />
              <span style={{ width: `${[45, 30][i]}%` }} />
            </div>
          ))
        ) : (
          <>
            {shown.map((p) =>
              row(
                p.id,
                <>
                  <span className={styles.rowName}>{p.name}</span>
                  {p.default_folder && (
                    <code className={styles.rowFolder}>{shortCwd(p.default_folder)}</code>
                  )}
                  {p.id === starredId && (
                    <span className={styles.star} title="Your default project">
                      ★<span className="sr-only"> default</span>
                    </span>
                  )}
                </>,
                { color: p.color },
              ),
            )}
          </>
        )}
        </div>
        <Link
          to={newProjectTo}
          state={newProjectState}
          className={styles.newProject}
          onClick={onNewProject}
        >
          + New project…
        </Link>
        {failed && (
          <p className={`${styles.note} ${styles.listNote}`} role="status">
            {projects === null
              ? "Couldn’t load your projects — you can still start without one."
              : "Couldn’t refresh your projects — showing the last list."}
          </p>
        )}
      </div>
    </div>
  );
}

export type FolderState =
  | "project-default"
  | "override"
  | "global-default"
  | "none";

/** Where the session starts — ONE line under the project list (#1473). The project rows
 *  already show each project's folder, so this only says what applies and offers the override; a
 *  full folder section repeated the row above it and cost a screenful on a phone. */
export function FolderLine({
  cwd,
  state,
  projectName,
  onChoose,
  onReset,
}: {
  cwd: string;
  state: FolderState;
  projectName: string;
  onChoose: (e: MouseEvent<HTMLButtonElement>) => void;
  onReset: () => void;
}) {
  const note =
    state === "project-default"
      ? "project default"
      : state === "override"
        ? "just this session"
        : state === "global-default"
          ? projectName
            ? `“${projectName}” has no default folder · your global default`
            : "your global default"
          : null;
  return (
    <div className={styles.folderLine} data-testid="launch-folder" data-path={cwd}>
      {cwd ? (
        <p className={styles.folderText}>
          Starts in <code title={cwd}>{shortCwd(cwd)}</code>
          {note && <span className={styles.folderNote}> · {note}</span>}
          {state === "override" && projectName && (
            <>
              {" · "}
              <button type="button" className={styles.linkButton} onClick={onReset}>
                use project default
              </button>
            </>
          )}
        </p>
      ) : (
        <p className={styles.error} role="status">
          {projectName
            ? `“${projectName}” has no default folder — choose one for this session.`
            : "Choose a folder for this session."}
        </p>
      )}
      <button type="button" className={styles.choose} onClick={onChoose}>
        Choose folder…
      </button>
    </div>
  );
}
