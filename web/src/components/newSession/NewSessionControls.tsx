import {
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Link } from "react-router-dom";
import { engineColor, engineInfo, engineLabel } from "../../app/engineRoster";
import { shortCwd } from "../../lib/format";
import type { ProjectPickerEntity } from "../../lib/projectPickerStore";
import type { UnavailableClient } from "../../types/api";
import { familyKeyOf, groupFamilies, type AgentFamily } from "./agentFamilies";
import styles from "./NewSessionControls.module.css";

/** The New session form's pickers (#1436): the agent tiles and the project list. Each is a group of
 *  NATIVE radios drawn as tiles or rows, so arrow keys, Tab order and screen-reader semantics are
 *  the browser's own — the visible box is only the radio's label. Which agents and projects exist is
 *  the caller's business: these never name one. There is no folder picker here (#1458): the project
 *  owns the launch folder, and the start summary names it. */

/** An agent tile's name: the agent itself (its console client's label) when the roster knows it,
 *  else the one client it has. */
function familyLabel(fam: AgentFamily): string {
  if (engineInfo(fam.key)) return engineLabel(fam.key);
  const first = fam.offered[0];
  return first ? engineLabel(first) : (fam.unavailable[0]?.label ?? fam.key);
}

/** How a client runs, as its segment says it: `API`, `Chat` or `Terminal`. A family with more than
 *  one API client names each by its own label. */
function modeLabel(id: string, fam: AgentFamily): string {
  const runtime = engineInfo(id)?.runtime;
  if (runtime === "chat") return "Chat";
  if (runtime === "api") {
    const apis = [...fam.offered, ...fam.unavailable.map((c) => c.id)].filter(
      (c) => engineInfo(c)?.runtime === "api",
    );
    return apis.length > 1 ? engineLabel(id) : "API";
  }
  return "Terminal";
}

/** What a mode segment says under its name. */
function modeSub(id: string): string {
  const info = engineInfo(id);
  if (info?.runtime === "chat") return "no tools";
  if (info?.runtime === "api") return "structured";
  return "full TUI";
}

/** What an agent tile says under its name: the ways it can run here. */
function familySub(fam: AgentFamily): string {
  if (fam.offered.length === 1 && engineInfo(fam.offered[0])?.runtime === "chat")
    return "chat · no tools";
  const modes = [...fam.offered, ...fam.unavailable.map((c) => c.id)]
    .sort((a, b) => Number(engineInfo(b)?.runtime === "api") - Number(engineInfo(a)?.runtime === "api"))
    .map((id) => modeLabel(id, fam));
  return [...new Set(modes)].join(" · ");
}

export function AgentTiles({
  engines,
  unavailable,
  value,
  defaultEngine,
  projectEngine = "",
  marks = {},
  familyDefaults,
  onFamily,
  onChange,
  onReaffirm,
}: {
  /** The clients that can start a session here, in roster order. */
  engines: string[];
  unavailable: UnavailableClient[];
  value: string;
  defaultEngine: string;
  /** The selected project's own agent (#1508), badged `project` — it outranks the global default. */
  projectEngine?: string;
  /** Each family's preselected client (#1728): `default` (its API client is ready), `default here`
   *  (the console stands in for an unavailable API client) or `remembered` (this device's own
   *  pick), with a line saying why. The server's family decision, never derived here. Shown on the
   *  client's mode segment. */
  marks?: Record<string, { word: string; sub?: string }>;
  /** Family key → the client a tap on its tile selects (the caller's family decision: API first
   *  where it can start). A family missing here selects its first offered client. */
  familyDefaults: Record<string, string>;
  /** A tap on another agent's tile, by family key: its preselected client is the caller's to
   *  resolve (live) — not an explicit mode pick. */
  onFamily: (familyKey: string) => void;
  /** A mode segment picked: an explicit choice of that client. */
  onChange: (id: string) => void;
  /** A tap on the mode segment that is ALREADY selected (#1728): the operator choosing the
   *  preselected client explicitly — e.g. Terminal while it stands in for an unready API client —
   *  so the pick can be remembered. Nothing else about the form changes (a radio fires no change). */
  onReaffirm?: (id: string) => void;
}) {
  const modeHead = useId();
  const families = groupFamilies(engines, unavailable);
  const selected = families.find((f) => f.offered.includes(value));
  const projectFamily = projectEngine ? familyKeyOf(projectEngine) : "";
  const defaultFamily = defaultEngine ? familyKeyOf(defaultEngine) : "";

  const tile = (fam: AgentFamily) => {
    const target = familyDefaults[fam.key] || fam.offered[0];
    const down = !target;
    const colorId = engineInfo(fam.key) ? fam.key : (fam.offered[0] ?? fam.key);
    return (
      <label
        key={fam.key}
        className={styles.tile}
        data-engine={fam.key}
        data-clients={[...fam.offered, ...fam.unavailable.map((c) => c.id)].join(" ")}
        data-testid={down ? "new-session-unavailable" : undefined}
        // The tile's rail is the agent's identity colour (NIGHTJAR, #1531) — never a state.
        style={down ? undefined : ({ "--eng": engineColor(colorId) } as CSSProperties)}
      >
        <input
          type="radio"
          name="new-session-family"
          value={down ? `unavailable:${fam.key}` : fam.key}
          checked={selected?.key === fam.key}
          disabled={down}
          onChange={() => target && onFamily(fam.key)}
        />
        <span className={styles.tileName}>
          {down && <span className={styles.ledDown} aria-hidden="true" />}
          {familyLabel(fam)}
          {!down &&
            (fam.key === projectFamily ? (
              <span className={styles.badge}>project</span>
            ) : (
              fam.key === defaultFamily && !projectEngine && <span className={styles.badge}>default</span>
            ))}
        </span>
        {down ? (
          // A client that cannot start is listed, disabled, with its reason ON the tile — its
          // absence is explained, never silent (#1311), without a separate box.
          <span className={styles.tileSub}>
            <span className="sr-only">unavailable: </span>
            {fam.unavailable.map((c) => c.reason).join("; ")}
          </span>
        ) : (
          <span className={styles.tileSub}>{familySub(fam)}</span>
        )}
      </label>
    );
  };

  const segment = (id: string, fam: AgentFamily) => {
    const mark = id === projectEngine ? undefined : marks[id];
    return (
      <label key={id} className={styles.mode} data-mode={engineInfo(id)?.runtime ?? ""}>
        <input
          type="radio"
          name="new-session-agent"
          value={id}
          checked={value === id}
          onChange={() => onChange(id)}
          onClick={() => {
            if (value === id) onReaffirm?.(id);
          }}
        />
        <span className={styles.modeName}>
          {modeLabel(id, fam)}
          {mark && (
            <span className={styles.badge} data-testid="new-session-family-mark">
              {mark.word}
            </span>
          )}
        </span>
        <span className={styles.tileSub}>{modeSub(id)}</span>
      </label>
    );
  };

  // API first: the mode row lists a family's API client(s) before its terminal.
  const apiFirst = (ids: string[]) =>
    [...ids].sort(
      (a, b) => Number(engineInfo(b)?.runtime === "api") - Number(engineInfo(a)?.runtime === "api"),
    );
  const selectedMark = selected ? marks[value] : undefined;

  return (
    <div className={styles.group}>
      <div role="radiogroup" aria-label="Agent" className={styles.tiles}>
        {families.map(tile)}
      </div>
      {selected && (
        <div className={styles.modeRow} data-testid="new-session-mode">
          <span className={styles.groupHead} id={modeHead}>
            Run {familyLabel(selected)} as
          </span>
          <div role="radiogroup" aria-labelledby={modeHead} className={styles.modes}>
            {apiFirst(selected.offered).map((id) => segment(id, selected))}
            {selected.unavailable.map((c) => (
              <label
                key={`unavailable:${c.id}`}
                className={styles.mode}
                data-testid="new-session-unavailable"
              >
                <input type="radio" name="new-session-agent" value={`unavailable:${c.id}`} disabled />
                <span className={styles.modeName}>
                  <span className={styles.ledDown} aria-hidden="true" />
                  {modeLabel(c.id, selected)}
                </span>
                <span className={styles.tileSub}>
                  <span className="sr-only">unavailable: </span>
                  {c.reason}
                </span>
              </label>
            ))}
          </div>
          {value !== projectEngine && selectedMark?.sub && (
            <span className={styles.tileSub}>{selectedMark.sub}</span>
          )}
        </div>
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
  leavesMap = false,
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
  /** In the map's New session modal (#1648): the wizard is a full page, so say it leaves the map. */
  leavesMap?: boolean;
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
      // sidebar or a dialog beside the form (#1437 review)…
      // Never while a modal is open — whatever has focus: a click on a dialog's padding drops it to
      // the body (#1437 reviews).
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
          two-project list and a forty-project list all take the same room, so nothing below the
          list moves when it lands. */}
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
              <span className="nj-skeleton" />
              <span className="nj-skeleton" style={{ width: `${[45, 30][i]}%` }} />
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
          {leavesMap && <span className={styles.leavesMap}> ↗ leaves the map</span>}
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
