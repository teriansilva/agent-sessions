import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useConfig } from "../app/config";
import { MAP_PATH, useMapWindows } from "../app/workspaceWindows";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { ApiError, api } from "../lib/api";
import { mintNewSessionId } from "../lib/newSession";
import {
  readLandingRestore,
  type NewSessionDraft,
  type WizardEntryState,
} from "../lib/newProject";
import { NEW_PROJECT_PATH } from "../lib/routes";
import {
  engineInfo,
  engineLabel,
  engineName,
  mintsOwnId,
  offeredModels,
  resolveDefault,
  useEngineRoster,
} from "../app/engineRoster";
import { unavailableDefaultNotice } from "../lib/agentDefaults";
import { owningProjectId } from "../lib/projectTree";
import type { ProjectEntity } from "../types/api";
import styles from "./NewSessionLanding.module.css";

/** Landing at "/" — no session selected. Pick an engine + project + folder and start a new
 *  session (#448): a project owns a DEFAULT launch folder, so choosing a project prefills the
 *  folder; the folder is overridable for this one session via a ~/-rooted picker. We mint a
 *  client-side id, navigate to /s/:engine/:id carrying the fresh-launch params (cwd + bypass).
 *
 *  Unless the map sent us (#936): pressing "+ New session" with the workspace up carries
 *  `returnTo: "/overview"` in router state, and then the launch goes back to the MAP as a window
 *  rather than taking over the screen. The form itself stays a page either way — it wants the
 *  room, and a 720×480 window has none to spare. */
/** The structured create whose outcome is unknown (#1311), per tab. */
const PENDING_CREATE = "battlelab.pendingStructuredCreate";
type PendingCreate = { engine: string; cwd: string; id: string };

function readPendingCreate(): PendingCreate | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(PENDING_CREATE) ?? "null") as PendingCreate | null;
    return v && typeof v.engine === "string" && typeof v.cwd === "string" && typeof v.id === "string"
      ? v
      : null;
  } catch {
    return null;
  }
}

function writePendingCreate(v: PendingCreate | null): void {
  try {
    if (v) sessionStorage.setItem(PENDING_CREATE, JSON.stringify(v));
    else sessionStorage.removeItem(PENDING_CREATE);
  } catch {
    /* storage unavailable: the in-flight guard still prevents a same-page double create */
  }
}

export function NewSessionLanding() {
  const config = useConfig();
  const navigate = useNavigate();
  const location = useLocation();
  const workspace = useMapWindows();
  // Where the operator pressed the button, recorded in router state by the sidebar. Note it is
  // NOT `mapReady`: this form replaced the map, so by the time it renders the map is unmounted
  // and `mapReady` is already false. The request survives that because it is queued on the
  // provider above the router, and the canvas drains it when it comes back.
  const returnToMap =
    (location.state as { returnTo?: string } | null)?.returnTo === MAP_PATH;
  // Back from the New project wizard (#1187): its router state restores this form as the operator
  // left it, and — on finish only — selects the project just created. Read ONCE, on mount.
  const [restore] = useState(() => readLandingRestore(location.state));
  const [engineChoice, setEngineChoice] = useState(
    restore.draft?.engineChoice ?? "",
  );
  // `null` = untouched: the stored default applies (#1128), and `true` — today's behaviour — until
  // the config has loaded. The operator's choice on this form always wins, for this session.
  const [bypassChoice, setBypass] = useState<boolean | null>(
    restore.draft?.bypassChoice ?? null,
  );
  const bypass = bypassChoice ?? config?.agent_defaults?.bypass ?? true;

  const roster = useEngineRoster();
  const engines = config?.new_session_engines ?? [];
  // The stored default agent, resolved through THE resolver (#1128): used when it can start a new
  // session here, else the first engine that can — and the notice below says so.
  const defaultChoice = resolveDefault(
    roster.engines.filter((e) => engines.includes(e.id)),
    config?.agent_defaults?.default_engine,
    "new",
  );
  const engine = engineChoice || defaultChoice.engine || engines[0] || "";
  const runtimeOf = (id: string) => engineInfo(id)?.runtime;
  const apiEngines = engines.filter((id) => runtimeOf(id) === "api" || runtimeOf(id) === "chat");
  const consoleEngines = engines.filter((id) => !apiEngines.includes(id));
  const unavailable = config?.unavailable_clients ?? [];
  const isApi = runtimeOf(engine) === "api";
  const apiSource = engineInfo(engine)?.api?.source ?? "";

  // The model (#1189), BOUND to the engine it was chosen for: switching engine — by the select or
  // by the default resolving differently — reads as `default` again, never as a model the other
  // engine happens to share a name with. A choice the server no longer offers is sent as chosen and
  // refused there (4422), never quietly swapped for `default` here.
  const [modelChoice, setModelChoice] = useState<{ engine: string; model: string } | null>(
    restore.draft?.modelChoice ?? null,
  );
  const model = modelChoice?.engine === engine ? modelChoice.model : "default";
  const models = offeredModels(engine);
  const modelSelect = engineInfo(engine)?.model_select;

  // Project entities own the default launch folder (#448). projectChoice === null = untouched
  // (use the default selection); "" = no project; else an entity id.
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  // A created project wins; otherwise the draft's own choice comes back in all three states.
  const [projectChoice, setProjectChoice] = useState<string | null>(
    restore.selectProjectId ?? restore.draft?.projectChoice ?? null,
  );
  // The operator's starred project (#615 Phase 2). `entities` is already archived-filtered, so an
  // id naming an archived — or since-deleted — project simply isn't found, and we fall back to the
  // first project rather than pre-selecting nothing. Before #615 there was no pref at all: the
  // preselection was `entities[0]`, i.e. whichever project sorted first by name.
  const starredId = config?.default_project_id ?? "";
  const starredExists = entities.some((p) => p.id === starredId);
  const defaultProjectId = (starredExists ? starredId : entities[0]?.id) ?? "";
  const projectSel = projectChoice ?? defaultProjectId;
  const selectedProject = entities.find((p) => p.id === projectSel);

  // Folder: the project's default unless overridden for this session via the picker.
  // A created project brings its own folder, so a finish never restores the old override.
  const [cwdOverride, setCwdOverride] = useState<string | null>(
    restore.selectProjectId ? null : (restore.draft?.cwdOverride ?? null),
  );

  // The restore is one-shot: drop it from the history entry (keeping the map return), so a later
  // Back/Forward onto this entry does not re-select the project or re-apply the draft.
  useEffect(() => {
    if (!restore.draft && !restore.selectProjectId) return;
    navigate(location.pathname, {
      replace: true,
      state: returnToMap ? { returnTo: MAP_PATH } : null,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, []);
  // `||`, not `??`, between the project's folder and the legacy pref: a folderless project stores
  // "" (#448 back-compat), and "" is a *missing* folder, not a chosen one — fall through to the
  // legacy cwd rather than opening the picker on nothing (#615 Phase 2 edge case). `cwdOverride`
  // keeps `??`: an explicit "" from the picker is a real choice.
  const projectCwd =
    selectedProject?.default_folder || config?.default_project || "";
  const cwd = cwdOverride ?? projectCwd;
  const isProjectDefault =
    !!selectedProject && cwd === selectedProject.default_folder && cwd !== "";

  // The folder picker overrides this session's folder. `false` = closed.
  const [picker, setPicker] = useState(false);
  // Captured at open time (not read from a ref during render) so focus returns to the trigger.
  const [pickerReturn, setPickerReturn] = useState<HTMLElement | null>(null);
  const openPicker = (e: { currentTarget: HTMLElement }) => {
    setPickerReturn(e.currentTarget);
    setPicker(true);
  };

  // Creating a project is the New project wizard's job (#1187). It carries this form's draft so
  // finishing — or cancelling — brings the form back exactly as it is now.
  const draft: NewSessionDraft = {
    engineChoice,
    bypassChoice,
    returnTo: returnToMap ? MAP_PATH : null,
    projectChoice,
    cwdOverride,
    ...(modelChoice ? { modelChoice } : {}),
  };
  const wizardState: WizardEntryState = { from: "new-session", draft };
  // …and the browser's Back too: before pushing the wizard, the draft is written into THIS entry's
  // state, so returning to it (Back, a phone's back gesture) restores the form as well. The replace
  // is awaited — under the data router `navigate` resolves once it commits — because a push issued
  // in the same tick would interrupt it.
  const openWizard = async (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    await navigate(location.pathname, {
      replace: true,
      state: {
        ...(returnToMap ? { returnTo: MAP_PATH } : {}),
        restoreDraft: draft,
      },
    });
    await navigate(NEW_PROJECT_PATH, { state: wizardState });
  };

  const refreshEntities = () =>
    api
      .projectEntities()
      .then((r) => setEntities(r.projects.filter((p) => !p.archived)))
      .catch(() => {});

  useEffect(() => {
    refreshEntities();
  }, []);

  // Nothing launches on a guessed capability (#853 P4): until the roster says whether this engine
  // pins or adopts its id, Start waits. `roster` is read so this re-evaluates when it lands.
  const mintKnown = roster.loaded && mintsOwnId(engine) !== undefined;
  const canStart = Boolean(engine && cwd && mintKnown);

  const [startError, setStartError] = useState<string | null>(null);
  // Chat creation is single-flight (Hermes on #1219): each `/api/chat/new` mints and persists a
  // conversation, so a second click while one is pending would create a duplicate. The ref is
  // the reservation (set before the await, so a same-tick double click is caught); the state
  // disables the button. `mounted` stops a late completion from navigating after the operator
  // has left the form.
  const chatStarting = useRef(false);
  const [startingChat, setStartingChat] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // A `chat` engine (#1209) launches nothing: its conversation is created on the server, which
  // mints the id, and the session opens like any other.
  const startChat = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    try {
      const { id: key } = await api.chatNew(engine, cwd);
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
        workspace.requestOpen({ key, engine, id, title: "New session" });
        navigate(MAP_PATH);
      } else {
        navigate(`/s/${engine}/${id}`);
      }
      const owningId = owningProjectId(cwd, entities);
      if (projectSel && projectSel !== owningId) {
        api.setSessionProject(key, projectSel).catch(() => {});
      }
    } catch (e) {
      if (!mounted.current) return;
      setStartError(
        e instanceof ApiError && e.message ? e.message : "Couldn’t start that conversation.",
      );
    } finally {
      chatStarting.current = false;
      if (mounted.current) setStartingChat(false);
    }
  };

  // A native API client (#1311) is created on the server too, through the structured route. The
  // operation id is the create's identity: a retry after an unknown outcome reuses it (the server
  // returns the same session), and a different agent or folder is a different create. The
  // unresolved attempt is kept in sessionStorage, so a reload after a lost response retries the
  // SAME create instead of starting a second session (Hermes on #1315).
  const startStructured = async () => {
    if (chatStarting.current) return;
    chatStarting.current = true;
    setStartingChat(true);
    setStartError(null);
    const prev = readPendingCreate();
    const attempt =
      prev && prev.engine === engine && prev.cwd === cwd
        ? prev
        : { engine, cwd, id: crypto.randomUUID() };
    writePendingCreate(attempt);
    try {
      const { session_key: key } = await api.structuredCreate(engine, cwd, attempt.id);
      writePendingCreate(null);
      if (!mounted.current) return;
      const id = key.slice(key.indexOf(":") + 1);
      if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
        workspace.requestOpen({ key, engine, id, title: "New session" });
        navigate(MAP_PATH);
      } else {
        navigate(`/s/${engine}/${id}`);
      }
      const owningId = owningProjectId(cwd, entities);
      if (projectSel && projectSel !== owningId) {
        api.setSessionProject(key, projectSel).catch(() => {});
      }
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) writePendingCreate(null); // refused
      if (!mounted.current) return;
      setStartError(
        e instanceof ApiError && e.message ? e.message : "Couldn’t start that session.",
      );
    } finally {
      chatStarting.current = false;
      if (mounted.current) setStartingChat(false);
    }
  };

  const start = () => {
    if (!canStart) return;
    if (engineInfo(engine)?.runtime === "api") {
      void startStructured();
      return;
    }
    if (engineInfo(engine)?.runtime === "chat") {
      void startChat();
      return;
    }
    const id = mintNewSessionId(engine);
    if (!id) return;
    const fresh = model === "default" ? { cwd, bypass } : { cwd, bypass, model };
    // Back to the map as a window, when that is where this came from AND the workspace has room
    // under the operator's cap. The request is queued on the workspace and drained by the canvas
    // once it has mounted and measured — the anchor and the overlay box are facts only the map
    // has, so it is the map that opens the window.
    //
    // The `hasRoom` half is not a nicety: at capacity the map would refuse the open and raise a
    // notice, and the launch — with the cwd and bypass choice just made on this form — would be
    // gone (Hermes on #939, finding 2). Starting the session is the operator's actual intent, so
    // a full workspace falls back to the ordinary full-screen launch rather than losing it. Note
    // it does NOT consult `mapReady`: this form replaced the map, so that is already false.
    if (returnToMap && workspace?.requestOpen && workspace.hasRoom) {
      workspace.requestOpen(
        { key: `${engine}:${id}`, engine, id, title: "New session" },
        fresh,
      );
      navigate(MAP_PATH);
    } else {
      navigate(`/s/${engine}/${id}`, { state: { fresh } });
    }
    // Stamp the project only when it's an explicit choice folder-resolution wouldn't already
    // produce (#361): a redundant owning entity isn't stamped; "" never stamps.
    const owningId = owningProjectId(cwd, entities);
    if (projectSel && projectSel !== owningId) {
      api.setSessionProject(`${engine}:${id}`, projectSel).catch(() => {});
    }
  };

  const onPick = (path: string) => {
    setCwdOverride(path);
    setPicker(false);
  };

  return (
    <div className={styles.landing}>
      <div className={styles.card}>
        <div className={styles.brandHero}>
          <div className={styles.wordmark}>
            Battle<b>Lab</b>
          </div>
          <p className={styles.tagline}>Command &amp; Code</p>
        </div>
        <h1>Start a new session</h1>

        {engines.length > 1 && (
          <label className={styles.field}>
            <span>Agent</span>
            <select
              value={engine}
              onChange={(e) => {
                setEngineChoice(e.target.value);
                setModelChoice(null); // a model belongs to the engine it was chosen for
              }}
            >
              {/* Console (a terminal) vs API (structured, no terminal) — #1311. A client that
                  cannot start is listed, disabled, so its absence is explained, not silent. */}
              <optgroup label="Console — terminal">
                {consoleEngines.map((id) => (
                  <option key={id} value={id}>
                    {engineName(id)}
                  </option>
                ))}
              </optgroup>
              {(apiEngines.length > 0 || unavailable.length > 0) && (
                <optgroup label="API — structured, no terminal">
                  {apiEngines.map((id) => (
                    <option key={id} value={id}>
                      {engineLabel(id)}
                    </option>
                  ))}
                  {unavailable.map((c) => (
                    <option key={c.id} value={`unavailable:${c.id}`} disabled>
                      {c.label} — unavailable
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
        )}
        {isApi && (
          <div className={styles.about} data-testid="new-session-api-about">
            <div className={styles.aboutHead}>
              <span className={`${styles.led} ${styles.ledUp}`} aria-hidden="true" />
              API client · ready
            </div>
            <p>
              BattleLab drives your installed <b>{engineLabel(apiSource)}</b> CLI through its
              structured protocol: its login, config, MCP servers and skills apply. No terminal.
              You answer each request it makes in the session view.
            </p>
          </div>
        )}
        {unavailable.length > 0 && (
          <div className={`${styles.about} ${styles.unavailable}`} data-testid="new-session-unavailable">
            {unavailable.map((c) => (
              <p key={c.id}>
                <span className={`${styles.led} ${styles.ledDown}`} aria-hidden="true" />
                <b>{c.label}</b> is unavailable: <span className={styles.reason}>{c.reason}</span>
              </p>
            ))}
          </div>
        )}

        {models.length > 0 ? (
          <label className={styles.field}>
            <span>Model</span>
            <select
              aria-label="Model"
              data-testid="new-session-model"
              value={model}
              onChange={(e) => setModelChoice({ engine, model: e.target.value })}
            >
              <option value="default">default</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.aliases.length ? `${m.id} (${m.aliases.join(", ")})` : m.id}
                </option>
              ))}
              {/* A restored choice the roster no longer lists stays visible, so what is shown is
                  what will be sent — and refused, rather than silently becoming `default`. */}
              {model !== "default" && !models.some((m) => m.id === model) && (
                <option value={model}>{model} (no longer offered)</option>
              )}
            </select>
          </label>
        ) : modelSelect?.configured_elsewhere && engineInfo(engine)?.runtime !== "chat" ? (
          <p className={styles.hint} data-testid="new-session-model-elsewhere">
            Model: set in {engineName(engine)}’s own configuration.
          </p>
        ) : null}

        {roster.loaded && defaultChoice.unavailableDefault && (
          <p className={styles.hint} role="status">
            {unavailableDefaultNotice(
              defaultChoice.unavailableDefault,
              defaultChoice.engine,
            )}
          </p>
        )}

        {/* Project FIRST (#448): it owns the default launch folder below. */}
        {entities.length > 0 && (
          <label className={styles.field}>
            <span>Project</span>
            <select
              aria-label="Project"
              value={projectSel}
              onChange={(e) => {
                setProjectChoice(e.target.value);
                setCwdOverride(null); // folder follows the newly-selected project's default
              }}
            >
              <option value="">no project</option>
              {entities.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <Link
          to={NEW_PROJECT_PATH}
          state={wizardState}
          className={styles.setDefault}
          onClick={(e) => void openWizard(e)}
        >
          + New project…
        </Link>

        {/* Folder BELOW the project (#448): prefilled from the project's default, overridable. */}
        <label className={styles.field}>
          <span>Folder</span>
          <div className={styles.folderRow}>
            <input
              type="text"
              readOnly
              aria-label="Launch folder"
              className={styles.folderPath}
              value={cwd || "no folder selected"}
            />
            <button
              type="button"
              className={styles.newFolderBtn}
              onClick={openPicker}
            >
              Choose folder…
            </button>
          </div>
        </label>
        {isProjectDefault ? (
          <p className={styles.hint}>
            ✓ default folder for “{selectedProject?.name}” — change it just for
            this session
          </p>
        ) : selectedProject &&
          !selectedProject.default_folder &&
          !cwdOverride ? (
          <p className={styles.error}>
            “{selectedProject.name}” has no default folder — choose one for this
            session.
          </p>
        ) : null}

        {/* A `chat` agent (#1209) has no tools, so there are no permission prompts to skip. An
            `api` client (#1311) never skips them: you answer each request in the session view,
            and its create route takes no bypass at all. */}
        {runtimeOf(engine) !== "chat" && runtimeOf(engine) !== "api" && (
        <label className={styles.checkbox}>
          <input
            type="checkbox"
            checked={bypass}
            onChange={(e) => setBypass(e.target.checked)}
          />
          <span>Skip permission prompts</span>
        </label>
        )}

        <button
          type="button"
          className={`${styles.start} shine`}
          disabled={!canStart || startingChat}
          onClick={start}
        >
          Start session
        </button>
        {startError && (
          <p className={styles.hint} role="alert" data-testid="start-error">
            {startError}
          </p>
        )}
        <p className={styles.hint}>
          …or open an existing session from the list.
        </p>
      </div>

      {picker && (
        <FolderPickerModal
          initialPath={cwd || undefined}
          title="Choose a folder"
          onPick={onPick}
          onCancel={() => setPicker(false)}
          returnFocusTo={pickerReturn}
        />
      )}
    </div>
  );
}
